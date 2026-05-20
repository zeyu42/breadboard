package controllers;

import akka.actor.ActorRef;
import akka.actor.Props;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.JsonNodeFactory;
import com.fasterxml.jackson.databind.node.ObjectNode;
import models.*;
import play.Play;
import play.libs.Akka;
import play.libs.Json;
import play.mvc.Controller;
import play.mvc.Result;
import play.mvc.Security;

import java.io.File;
import java.text.SimpleDateFormat;
import java.util.Collections;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.TimeUnit;

/**
 * Read-only debug endpoints used by the Breadboard MCP server so an LLM agent
 * can inspect experiments, instances and events as JSON, and (optionally)
 * evaluate Groovy in the running script engine.
 *
 * All endpoints require an authenticated admin session (use POST /login first).
 */
public class DebugController extends Controller {

  private static final SimpleDateFormat DATE_FMT =
      new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'");

  /**
   * Idempotent schema fix for fresh databases: evolution 28 creates an
   * empty `breadboard_version` table, which short-circuits
   * Global.onStart()'s upgrade path so `experiments.file_mode` never
   * gets added. Run this once on a freshly-spawned Breadboard before any
   * /debug/* call that queries experiments. Safe to call multiple times.
   */
  public static Result bootstrapSchema() {
    com.avaje.ebean.Ebean.createSqlUpdate(
        "alter table experiments add column if not exists file_mode bit default 0;"
    ).execute();
    ObjectNode out = Json.newObject();
    out.put("status", "ok");
    return ok(out);
  }

  /**
   * JSON login: { "email": "...", "password": "..." } -> sets the Play
   * session cookie (uid, email) so subsequent /debug/* calls authenticate.
   *
   * Exists because Application.authenticate uses Form.bindFromRequest, which
   * doesn't pick up form-urlencoded bodies in this build (UI logs in via WS
   * instead). The MCP server uses this endpoint so it can drive Breadboard
   * over plain HTTP+JSON.
   */
  public static Result login() {
    JsonNode json = request().body().asJson();
    if (json == null) {
      return badRequest("Expecting JSON body with email + password");
    }
    String email = json.findPath("email").textValue();
    String password = json.findPath("password").textValue();
    if (email == null || password == null) {
      return badRequest("email and password are required");
    }
    User user = User.authenticate(email, password);
    if (user == null) {
      ObjectNode err = Json.newObject();
      err.put("status", "error");
      err.put("message", "Invalid username or password");
      return badRequest(err);
    }
    String uid = UUID.randomUUID().toString();
    // Ebean's user.update() turned out to be a no-op for the uid column on
    // this build (likely because the loaded entity has dirty-tracking issues
    // with lazy associations). Use a direct SQL update instead.
    com.avaje.ebean.Ebean.createSqlUpdate("update users set uid = :uid where email = :email")
        .setParameter("uid", uid)
        .setParameter("email", email)
        .execute();
    user.uid = uid;

    session("email", email);
    session("uid", uid);
    session("juid", uid);

    // Mirror the WS LogIn flow: ensure a ScriptBoard actor exists for this
    // user so subsequent /debug/select-experiment, /debug/launch-game, etc.
    // can drive the actor system.
    boolean actorCreated = false;
    if (!Breadboard.instances.containsKey(email)) {
      ActorRef scriptBoardController = Akka.system().actorOf(new Props(ScriptBoard.class));
      Breadboard.instances.put(email, scriptBoardController);
      scriptBoardController.tell(
          new Breadboard.AddAdmin(user, scriptBoardController, new NoopThrottledWebSocketOut()),
          null);
      actorCreated = true;
    }

    ObjectNode result = Json.newObject();
    result.put("uid", uid);
    result.put("email", email);
    result.put("actorCreated", actorCreated);
    return ok(result);
  }

  @Security.Authenticated(Secured.class)
  public static Result listExperiments() {
    String uid = session().get("uid");
    User user = User.findByUID(uid);
    ArrayNode out = JsonNodeFactory.instance.arrayNode();
    if (user != null) {
      for (Experiment e : user.ownedExperiments) {
        ObjectNode node = Json.newObject();
        node.put("id", e.id);
        node.put("name", e.name);
        node.put("uid", e.uid);
        node.put("fileMode", e.fileMode);
        node.put("instanceCount", e.instances == null ? 0 : e.instances.size());
        out.add(node);
      }
    }
    return ok(out);
  }

  @Security.Authenticated(Secured.class)
  public static Result getExperiment(Long experimentId) {
    Experiment experiment = Experiment.findById(experimentId);
    if (experiment == null) {
      return notFound("No experiment with id " + experimentId);
    }
    return ok(experiment.toJson());
  }

  @Security.Authenticated(Secured.class)
  public static Result listInstances(Long experimentId) {
    Experiment experiment = Experiment.findById(experimentId);
    if (experiment == null) {
      return notFound("No experiment with id " + experimentId);
    }
    ArrayNode out = JsonNodeFactory.instance.arrayNode();
    for (ExperimentInstance ei : experiment.instances) {
      out.add(ei.toJsonStub());
    }
    return ok(out);
  }

  @Security.Authenticated(Secured.class)
  public static Result getInstance(Long instanceId) {
    ExperimentInstance ei = ExperimentInstance.findById(instanceId);
    if (ei == null) {
      return notFound("No instance with id " + instanceId);
    }
    return ok(ei.toJsonStub());
  }

  /**
   * Return the instance's events as JSON, ordered by datetime.
   * Optional query params: limit (default 200), offset (default 0),
   * nameFilter (substring match on event name).
   */
  @Security.Authenticated(Secured.class)
  public static Result getInstanceEvents(Long instanceId) {
    ExperimentInstance ei = ExperimentInstance.findById(instanceId);
    if (ei == null) {
      return notFound("No instance with id " + instanceId);
    }
    int limit = 200;
    int offset = 0;
    String nameFilter = null;
    try {
      if (request().getQueryString("limit") != null) {
        limit = Integer.parseInt(request().getQueryString("limit"));
      }
      if (request().getQueryString("offset") != null) {
        offset = Integer.parseInt(request().getQueryString("offset"));
      }
      nameFilter = request().getQueryString("nameFilter");
    } catch (NumberFormatException nfe) {
      return badRequest("limit/offset must be integers");
    }

    List<Event> events = ei.events;
    Collections.sort(events, new Comparator<Event>() {
      @Override
      public int compare(Event a, Event b) {
        return a.datetime.compareTo(b.datetime);
      }
    });

    ObjectNode result = Json.newObject();
    result.put("instanceId", instanceId);
    result.put("totalEvents", events.size());
    ArrayNode arr = result.putArray("events");

    int seen = 0;
    int returned = 0;
    for (Event e : events) {
      if (nameFilter != null && !nameFilter.isEmpty() && !e.name.contains(nameFilter)) {
        continue;
      }
      if (seen++ < offset) continue;
      if (returned >= limit) break;
      arr.add(e.toJson());
      returned++;
    }
    result.put("returned", returned);
    result.put("offset", offset);
    result.put("limit", limit);
    return ok(result);
  }

  /**
   * Return whatever the user currently has selected in the admin session.
   * Useful for the MCP server to know which experiment/instance the script
   * engine is currently bound to before calling /debug/script.
   */
  @Security.Authenticated(Secured.class)
  public static Result getCurrentSelection() {
    String uid = session().get("uid");
    User user = User.findByUID(uid);
    ObjectNode out = Json.newObject();
    if (user == null) {
      return unauthorized("No user for session");
    }
    out.put("userEmail", user.email);
    if (user.selectedExperiment != null) {
      ObjectNode exp = Json.newObject();
      exp.put("id", user.selectedExperiment.id);
      exp.put("name", user.selectedExperiment.name);
      out.put("selectedExperiment", exp);
    } else {
      out.putNull("selectedExperiment");
    }
    if (user.experimentInstanceId != null && user.experimentInstanceId != -1L) {
      out.put("experimentInstanceId", user.experimentInstanceId);
      ExperimentInstance ei = ExperimentInstance.findById(user.experimentInstanceId);
      if (ei != null) {
        out.put("experimentInstanceStatus", ei.status.toString());
        out.put("experimentInstanceName", ei.name);
      }
    } else {
      out.putNull("experimentInstanceId");
    }
    return ok(out);
  }

  /**
   * Return the on-disk paths Breadboard uses for this experiment's file-mode
   * directory. The MCP server uses this to write files (Steps/*.groovy,
   * parameters.csv, client-*.html/js, style.css) directly into the right
   * location — the same thing `npm run serve` does in template projects.
   */
  @Security.Authenticated(Secured.class)
  public static Result getExperimentPaths(Long experimentId) {
    Experiment experiment = Experiment.findById(experimentId);
    if (experiment == null) {
      return notFound("No experiment with id " + experimentId);
    }
    String appRoot = Play.application().path().getAbsolutePath();
    String devDir = appRoot + "/dev/" + experiment.getDirectoryName();
    ObjectNode out = Json.newObject();
    out.put("experimentId", experimentId);
    out.put("name", experiment.name);
    out.put("directoryName", experiment.getDirectoryName());
    out.put("fileMode", experiment.fileMode);
    out.put("applicationPath", appRoot);
    out.put("devDirectory", devDir);
    out.put("stepsDir", devDir + "/steps");
    out.put("contentDir", devDir + "/Content");
    out.put("imagesDir", devDir + "/Images");
    out.put("parametersFile", devDir + "/parameters.csv");
    out.put("clientHtmlFile", devDir + "/client-html.html");
    out.put("clientGraphFile", devDir + "/client-graph.js");
    out.put("styleFile", devDir + "/style.css");
    return ok(out);
  }

  /**
   * Toggle (or set) file mode on an experiment. When turning on, exports the
   * current experiment into dev/<directoryName>/. When turning off, re-imports.
   * Body: { "enabled": true } (optional — if absent, toggles).
   */
  @Security.Authenticated(Secured.class)
  public static Result setFileMode(Long experimentId) {
    Experiment experiment = Experiment.findById(experimentId);
    if (experiment == null) {
      return notFound("No experiment with id " + experimentId);
    }
    String uid = session().get("uid");
    User user = User.findByUID(uid);
    if (user == null) {
      return unauthorized("No user for session");
    }
    JsonNode json = request().body().asJson();
    Boolean target = null;
    if (json != null && json.has("enabled") && !json.get("enabled").isNull()) {
      target = json.get("enabled").asBoolean();
    }
    if (target != null && experiment.fileMode != null && experiment.fileMode.equals(target)) {
      ObjectNode noop = Json.newObject();
      noop.put("experimentId", experimentId);
      noop.put("fileMode", experiment.fileMode);
      noop.put("changed", false);
      return ok(noop);
    }
    experiment.toggleFileMode(user);
    experiment.refresh();
    ObjectNode out = Json.newObject();
    out.put("experimentId", experimentId);
    out.put("fileMode", experiment.fileMode);
    out.put("changed", true);
    out.put("devDirectory",
        Play.application().path().getAbsolutePath() + "/dev/" + experiment.getDirectoryName());
    return ok(out);
  }

  /**
   * Set this admin user's currently-selected experiment (the value that
   * determines which experiment's engine bindings get used by /debug/script
   * after the engine is rebuilt). Lighter-weight than a full WS SelectExperiment
   * — does NOT itself rebuild the script engine; for that, the experimenter
   * still needs to load an instance via the UI for now.
   *
   * Body: { "experimentId": 7 }
   */
  @Security.Authenticated(Secured.class)
  public static Result setSelectedExperiment() {
    String uid = session().get("uid");
    User user = User.findByUID(uid);
    if (user == null) {
      return unauthorized("No user for session");
    }
    JsonNode json = request().body().asJson();
    if (json == null || !json.has("experimentId")) {
      return badRequest("Expecting JSON body with experimentId");
    }
    Long experimentId = json.get("experimentId").asLong();
    Experiment experiment = Experiment.findById(experimentId);
    if (experiment == null) {
      return notFound("No experiment with id " + experimentId);
    }
    user.setSelectedExperiment(experiment);
    user.update();
    ObjectNode out = Json.newObject();
    out.put("experimentId", experimentId);
    out.put("name", experiment.name);
    return ok(out);
  }

  // ---------------------------------------------------------- lifecycle

  /**
   * Tell breadboardController to switch this admin user to the given experiment,
   * which rebuilds the script engine and loads the experiment's steps into it.
   * Returns once the actor has produced its first output message, or 5s
   * timeout, whichever comes first.
   *
   * Body: { "experimentId": 33 }
   */
  @Security.Authenticated(Secured.class)
  public static Result selectExperiment() {
    String uid = session().get("uid");
    User user = User.findByUID(uid);
    if (user == null) return unauthorized("No user for session");
    JsonNode json = request().body().asJson();
    if (json == null || !json.has("experimentId")) {
      return badRequest("Expecting JSON body with experimentId");
    }
    Long experimentId = json.get("experimentId").asLong();
    Experiment experiment = Experiment.findById(experimentId);
    if (experiment == null) return notFound("No experiment with id " + experimentId);

    // Replicate what the breadboardController-side SelectExperiment handler
    // does, but skip that actor and talk directly to the per-user ScriptBoard
    // actor. (Bypassing breadboardController avoids a stale-classloader bug
    // in sbt + Play 2.2 dev mode where the controller actor was created
    // before our hot-reload and references an older Breadboard.instances map.)
    user.setSelectedExperiment(experiment);
    user.update();
    // Do NOT re-fetch User here — User.findByUID would re-load `user` with
    // `selectedExperiment` as a lazy proxy whose `name` and `fileMode` are
    // null until first access (and the proxy fields are observed-stale in
    // some places, eg user.toJson). Keep `experiment` as the in-memory fresh
    // reference so downstream code sees populated fields.

    ActorRef sb = Breadboard.instances.get(user.email);
    if (sb == null) {
      return badRequest("No ScriptBoard actor for user. Re-login.");
    }
    NoopThrottledWebSocketOut out = new NoopThrottledWebSocketOut();
    sb.tell(new Breadboard.ChangeExperiment(user, experiment, out), null);

    // The handler runs every Step into the engine, which can take a moment.
    awaitOrTimeout(out, 10000);

    ObjectNode result = Json.newObject();
    result.put("experimentId", experimentId);
    result.put("name", experiment.name);
    summarizeCaptured(result, out);
    return ok(result);
  }

  /**
   * Tell this user's ScriptBoard actor to launch a new game (creates and
   * starts an ExperimentInstance, runs the steps against it).
   *
   * Body: { "name": "...", "parameters": {...} } -- experimentId is implied
   *        from the user's selectedExperiment.
   */
  @Security.Authenticated(Secured.class)
  public static Result launchGame() {
    String uid = session().get("uid");
    User user = User.findByUID(uid);
    if (user == null) return unauthorized("No user for session");
    if (user.selectedExperiment == null) {
      return badRequest("No experiment selected. Call /debug/select-experiment first.");
    }
    JsonNode json = request().body().asJson();
    if (json == null || !json.has("name")) {
      return badRequest("Expecting JSON body with name (the instance name)");
    }
    String name = json.get("name").asText();

    LinkedHashMap<String, Object> parameters = new LinkedHashMap<>();
    if (json.has("parameters") && json.get("parameters").isObject()) {
      java.util.Iterator<java.util.Map.Entry<String, JsonNode>> it =
          json.get("parameters").fields();
      while (it.hasNext()) {
        java.util.Map.Entry<String, JsonNode> entry = it.next();
        parameters.put(entry.getKey(), entry.getValue().asText());
      }
    }

    ActorRef sb = Breadboard.instances.get(user.email);
    if (sb == null) {
      return badRequest("No ScriptBoard actor for user. Re-login.");
    }
    // Eager-load selectedExperiment so it has populated fields (not a lazy
    // proxy with null name/fileMode that breaks user.toJson() downstream).
    if (user.selectedExperiment != null) {
      user.selectedExperiment =
          Experiment.findById(user.selectedExperiment.id);
    }
    NoopThrottledWebSocketOut out = new NoopThrottledWebSocketOut();
    sb.tell(new Breadboard.LaunchGame(user, name, parameters, out), null);

    // The handler runs every Step source into the engine, which can take a
    // moment. Wait longer here.
    awaitOrTimeout(out, 15000);

    ObjectNode result = Json.newObject();
    result.put("experimentId", user.selectedExperiment.id);
    result.put("name", name);
    summarizeCaptured(result, out);

    // The user's experimentInstanceId is updated by the actor; refresh.
    user.refresh();
    if (user.experimentInstanceId != null && user.experimentInstanceId != -1L) {
      result.put("experimentInstanceId", user.experimentInstanceId);
    }
    return ok(result);
  }

  /**
   * Tell breadboardController to bind the script engine to an existing
   * instance. Needed if the engine has been re-initialized or to switch
   * between instances.
   *
   * Body: { "instanceId": 7 }
   */
  @Security.Authenticated(Secured.class)
  public static Result selectInstance() {
    String uid = session().get("uid");
    User user = User.findByUID(uid);
    if (user == null) return unauthorized("No user for session");
    JsonNode json = request().body().asJson();
    if (json == null || !json.has("instanceId")) {
      return badRequest("Expecting JSON body with instanceId");
    }
    Long instanceId = json.get("instanceId").asLong();

    // Bypass breadboardController for the same classloader reason as
    // selectExperiment(); the per-user actor handles SelectInstance directly.
    ActorRef sb = Breadboard.instances.get(user.email);
    if (sb == null) return badRequest("No ScriptBoard actor for user. Re-login.");
    NoopThrottledWebSocketOut out = new NoopThrottledWebSocketOut();
    sb.tell(new Breadboard.SelectInstance(user, instanceId, out), null);

    awaitOrTimeout(out, 10000);

    ObjectNode result = Json.newObject();
    result.put("instanceId", instanceId);
    summarizeCaptured(result, out);
    return ok(result);
  }

  /**
   * Tell this user's ScriptBoard actor to stop the running game.
   * Body: { "instanceId": 7 }
   */
  @Security.Authenticated(Secured.class)
  public static Result stopGame() {
    String uid = session().get("uid");
    User user = User.findByUID(uid);
    if (user == null) return unauthorized("No user for session");
    JsonNode json = request().body().asJson();
    if (json == null || !json.has("instanceId")) {
      return badRequest("Expecting JSON body with instanceId");
    }
    Long instanceId = json.get("instanceId").asLong();
    ActorRef sb = Breadboard.instances.get(user.email);
    if (sb == null) return badRequest("No ScriptBoard actor for user. Re-login.");
    NoopThrottledWebSocketOut out = new NoopThrottledWebSocketOut();
    sb.tell(new Breadboard.StopGame(user, instanceId, out), null);
    awaitOrTimeout(out, 5000);

    ObjectNode result = Json.newObject();
    result.put("instanceId", instanceId);
    return ok(result);
  }

  /**
   * Wait for the actor to settle: until no new messages have been captured
   * for `quietMillis` consecutive milliseconds, or `maxTotalMillis` total
   * elapses, whichever comes first.
   *
   * LaunchGame produces one ThrottledWebSocketOut.write() per Step it
   * processes (each Step source is run separately via processScript) plus
   * one for the trailing Update. For a 10-step experiment that's ~11
   * writes spaced over multiple seconds.
   */
  /**
   * Populate result with summary info from the captured actor messages:
   * total count, plus the captured `error` strings and the last few `output`
   * lines. This exposes the actual content of script-eval results back to
   * the MCP caller, which is what an LLM agent needs to understand why a
   * step-load or RunStep failed.
   */
  private static void summarizeCaptured(ObjectNode result, NoopThrottledWebSocketOut out) {
    List<JsonNode> msgs = out.captured();
    result.put("messagesCaptured", msgs.size());

    ArrayNode errors = result.putArray("errors");
    for (JsonNode m : msgs) {
      if (m.has("error") && m.get("error").isTextual()) {
        String err = m.get("error").asText();
        if (err != null && !err.isEmpty()) {
          ObjectNode e = JsonNodeFactory.instance.objectNode();
          if (m.has("scriptName") && m.get("scriptName").isTextual()) {
            e.put("scriptName", m.get("scriptName").asText());
          }
          e.put("error", err);
          errors.add(e);
        }
      }
    }
  }

  private static void awaitOrTimeout(NoopThrottledWebSocketOut out, long maxTotalMillis) {
    final long quietMillis = 1500;
    long start = System.currentTimeMillis();
    int lastCount = -1;
    long lastCountChangeAt = start;
    while (true) {
      long now = System.currentTimeMillis();
      if (now - start >= maxTotalMillis) break;
      int count = out.captured().size();
      if (count != lastCount) {
        lastCount = count;
        lastCountChangeAt = now;
      } else if (count > 0 && now - lastCountChangeAt >= quietMillis) {
        break;
      }
      try {
        Thread.sleep(100);
      } catch (InterruptedException ie) {
        Thread.currentThread().interrupt();
        break;
      }
    }
  }

  /**
   * Evaluate a Groovy script against the shared ScriptBoard engine and return
   * { output, error } synchronously. The engine reflects the experiment
   * instance currently loaded by the admin user (select one in the UI first
   * if you need experiment-specific bindings like g, a, c, ...).
   *
   * Request body: { "script": "g.V.count()" }
   */
  @Security.Authenticated(Secured.class)
  public static Result executeScript() {
    JsonNode json = request().body().asJson();
    if (json == null) {
      return badRequest("Expecting JSON body with { \"script\": \"...\" }");
    }
    JsonNode scriptNode = json.findPath("script");
    if (scriptNode.isMissingNode() || scriptNode.isNull()) {
      return badRequest("Missing required field: script");
    }
    String script = scriptNode.textValue();
    if (script == null || script.isEmpty()) {
      return badRequest("script field must be a non-empty string");
    }
    ObjectNode result = ScriptBoard.processScriptSync(script);
    return ok(result);
  }
}
