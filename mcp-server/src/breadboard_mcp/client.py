"""HTTP client for the Breadboard debug API.

The admin login at POST /login expects a form body (email, password) — the
existing Application.authenticate handler binds to a Form. It sets a Play
session cookie ("PLAY_SESSION") which carries the uid/email and is sent back
on subsequent requests; httpx.Client keeps that cookie automatically.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import httpx


class BreadboardClient:
    def __init__(
        self,
        base_url: str,
        email: str,
        password: str,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )
        self._logged_in = False

    # ------------------------------------------------------------ lifecycle
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "BreadboardClient":
        self.login()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --------------------------------------------------------------- auth
    def login(self) -> None:
        # Use the JSON-based /debug/login endpoint rather than the legacy
        # /login (which goes through Play 2.2's Form.bindFromRequest and is
        # only wired up for the WS-based admin UI).
        r = self._client.post(
            "/debug/login",
            json={"email": self.email, "password": self.password},
        )
        if r.status_code >= 400:
            raise RuntimeError(
                f"Breadboard login failed ({r.status_code}): {r.text[:200]}"
            )
        self._logged_in = True

    def _ensure_login(self) -> None:
        if not self._logged_in:
            self.login()

    def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        self._ensure_login()
        r = self._client.request(method, path, **kw)
        # Re-auth once if the session has expired.
        if r.status_code == 401:
            self._logged_in = False
            self._ensure_login()
            r = self._client.request(method, path, **kw)
        return r

    @staticmethod
    def _ok_json(r: httpx.Response) -> Any:
        if r.status_code >= 400:
            raise RuntimeError(f"Breadboard {r.request.method} {r.request.url.path} "
                               f"returned {r.status_code}: {r.text[:500]}")
        if not r.text:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text

    # ---------------------------------------------------------- debug API
    def list_experiments(self) -> list[dict]:
        return self._ok_json(self._request("GET", "/debug/experiments"))

    def get_experiment(self, experiment_id: int) -> dict:
        return self._ok_json(self._request("GET", f"/debug/experiments/{experiment_id}"))

    def list_instances(self, experiment_id: int) -> list[dict]:
        return self._ok_json(
            self._request("GET", f"/debug/experiments/{experiment_id}/instances")
        )

    def get_instance(self, instance_id: int) -> dict:
        return self._ok_json(self._request("GET", f"/debug/instances/{instance_id}"))

    def get_instance_events(
        self,
        instance_id: int,
        limit: int = 200,
        offset: int = 0,
        name_filter: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if name_filter:
            params["nameFilter"] = name_filter
        return self._ok_json(
            self._request("GET", f"/debug/instances/{instance_id}/events", params=params)
        )

    def get_current_selection(self) -> dict:
        selection = self._ok_json(self._request("GET", "/debug/selection"))
        experiment = selection.get("selectedExperiment")
        instance_id = selection.get("experimentInstanceId")
        selection["runtimeBindingVerified"] = bool(
            experiment
            and instance_id is not None
            and self._runtime_binding_matches(experiment["id"], instance_id)
        )
        return selection

    def _runtime_binding_matches(self, experiment_id: int, instance_id: int) -> bool:
        probe = self.execute_script(
            "def ei = eventTracker.getExperimentInstance(); "
            f"ei != null && ei.id == {int(instance_id)}L "
            f"&& ei.experiment?.id == {int(experiment_id)}L"
        )
        return not probe.get("error") and probe.get("output", "").rsplit("==>", 1)[-1].strip() == "true"

    def _require_runtime_binding(self, experiment_id: int, instance_id: int) -> dict:
        selection = self.get_current_selection()
        if (selection.get("experimentInstanceId") != instance_id
                or not selection["runtimeBindingVerified"]):
            raise RuntimeError(
                f"Breadboard did not bind experiment {experiment_id}, instance {instance_id}; "
                f"current selection: {selection}"
            )
        return selection

    def set_selected_experiment(self, experiment_id: int) -> dict:
        return self._ok_json(
            self._request("POST", "/debug/selection/experiment",
                          json={"experimentId": experiment_id})
        )

    def get_experiment_paths(self, experiment_id: int) -> dict:
        return self._ok_json(
            self._request("GET", f"/debug/experiments/{experiment_id}/paths")
        )

    def set_file_mode(self, experiment_id: int, enabled: bool | None = None) -> dict:
        body: dict[str, Any] = {}
        if enabled is not None:
            body["enabled"] = enabled
        return self._ok_json(
            self._request("POST", f"/debug/experiments/{experiment_id}/file-mode",
                          json=body)
        )

    # ---------------------------------------------------------- experiments

    def create_experiment(
        self,
        name: str,
        copy_experiment_id: int | None = None,
    ) -> dict:
        """Create a new experiment. Calls the existing PUT /experiment.

        Returns the newly-created experiment as JSON (including its id).
        """
        body: dict[str, Any] = {"newExperimentName": name}
        if copy_experiment_id is not None:
            body["copyExperimentId"] = copy_experiment_id
        else:
            body["copyExperimentId"] = 0  # the Java side does asLong(); use 0
        return self._ok_json(self._request("PUT", "/experiment", json=body))

    # --------------------------------------------------- filesystem sync

    # `npm run serve` in the v2.4 template copies `backend/<filename>` into the
    # breadboard server's `dev/<directoryName>/<filename>`, with a small
    # in-place transform on `client-graph.js`. We do the same locally on disk.

    _CLIENT_GRAPH_REPLACEMENTS_DEV = {
        "__DEV__": "true",
        "__PROD__": "false",
    }

    def sync_experiment_files(
        self,
        experiment_id: int,
        source_dir: str | os.PathLike[str],
        public_root: str = "/generated/",
        dev_mode: bool = True,
    ) -> dict:
        """Copy a directory tree of experiment files onto the Breadboard
        server's `dev/<directoryName>/` location for this experiment.

        This is the MCP equivalent of `npm run serve` in the v2.4 template:
        it expects `source_dir` to contain (any subset of) `Steps/`, `Content/`,
        `Images/`, `parameters.csv`, `client-html.html`, `client-graph.js`,
        `style.css`. `client-graph.js` is transformed in the same way the
        webpack CopyPlugin does (replacing __PUBLIC_ROOT__, __DEV__, __PROD__).

        Returns a manifest of files written.
        """
        paths = self.get_experiment_paths(experiment_id)
        dev_dir = Path(paths["devDirectory"])
        src = Path(source_dir).expanduser().resolve()
        if not src.is_dir():
            raise RuntimeError(f"source_dir does not exist or is not a directory: {src}")
        dev_dir.mkdir(parents=True, exist_ok=True)

        written: list[str] = []
        for entry in src.rglob("*"):
            if entry.is_dir():
                continue
            rel = entry.relative_to(src)
            # Breadboard reads steps from a lowercase 'steps/' directory
            # (Experiment.getSteps() in models/Experiment.java). The template
            # ships them in 'Steps/', so normalize.
            parts = list(rel.parts)
            if parts and parts[0] == "Steps":
                parts[0] = "steps"
            dst = dev_dir.joinpath(*parts)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if rel.name == "client-graph.js":
                text = entry.read_text(encoding="utf-8")
                text = text.replace("__PUBLIC_ROOT__", public_root)
                text = text.replace("__DEV__", "true" if dev_mode else "false")
                text = text.replace("__PROD__", "false" if dev_mode else "true")
                dst.write_text(text, encoding="utf-8")
            else:
                shutil.copy2(entry, dst)
            written.append(str(dst.relative_to(dev_dir)))

        return {
            "experimentId": experiment_id,
            "devDirectory": str(dev_dir),
            "filesWritten": written,
            "count": len(written),
        }

    def execute_script(self, script: str) -> dict:
        return self._ok_json(
            self._request("POST", "/debug/script", json={"script": script})
        )

    # ------------------------------------------------------------ lifecycle

    def select_experiment_for_engine(self, experiment_id: int) -> dict:
        """Bind the script engine to this experiment (rebuilds engine, loads
        all step source into it). Required before launch_game / select_instance
        will work usefully."""
        return self._ok_json(
            self._request("POST", "/debug/select-experiment",
                          json={"experimentId": experiment_id})
        )

    def launch_game(self, name: str, parameters: dict | None = None) -> dict:
        """Create + start a new ExperimentInstance of the currently-selected
        experiment, run all the steps. Returns once the actor settles or
        15s elapses."""
        body: dict = {"name": name}
        if parameters is not None:
            body["parameters"] = parameters
        experiment = self.get_current_selection().get("selectedExperiment")
        result = self._ok_json(
            self._request("POST", "/debug/launch-game", json=body)
        )
        if not experiment or result.get("experimentInstanceId") is None:
            raise RuntimeError(f"Breadboard launch did not return a bound instance: {result}")
        self._require_runtime_binding(experiment["id"], result["experimentInstanceId"])
        return result

    def select_instance_for_engine(self, instance_id: int) -> dict:
        """Bind the script engine to an existing ExperimentInstance."""
        experiment = self.get_current_selection().get("selectedExperiment")
        result = self._ok_json(
            self._request("POST", "/debug/select-instance",
                          json={"instanceId": instance_id})
        )
        if not experiment:
            raise RuntimeError("Select an experiment before binding an instance.")
        self._require_runtime_binding(experiment["id"], instance_id)
        return result

    def stop_game(self, instance_id: int) -> dict:
        selection = self.get_current_selection()
        if (selection.get("experimentInstanceId") != instance_id
                or not selection["runtimeBindingVerified"]):
            raise RuntimeError(
                "Refusing to stop an instance that is not the verified runtime binding. "
                "Breadboard clears the current instance selection even when stopping an "
                "unrelated instance. Bind and verify the target instance first."
            )
        return self._ok_json(
            self._request("POST", "/debug/stop-game",
                          json={"instanceId": instance_id})
        )

    # --------------------------------------------------------- CSV helpers
    def instance_data_csv(self, experiment_id: int) -> str:
        return self._ok_json(self._request("GET", f"/csv/instances/{experiment_id}"))

    def event_csv(self, instance_id: int) -> str:
        return self._ok_json(self._request("GET", f"/csv/data/{instance_id}"))


def from_env() -> BreadboardClient:
    base = os.environ.get("BREADBOARD_URL", "http://localhost:9000")
    email = os.environ.get("BREADBOARD_EMAIL")
    password = os.environ.get("BREADBOARD_PASSWORD")
    if not email or not password:
        raise RuntimeError(
            "Set BREADBOARD_EMAIL and BREADBOARD_PASSWORD env vars "
            "(BREADBOARD_URL is optional, defaults to http://localhost:9000)."
        )
    return BreadboardClient(base_url=base, email=email, password=password)
