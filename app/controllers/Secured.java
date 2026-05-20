package controllers;

import com.fasterxml.jackson.databind.node.ObjectNode;
import models.User;
import play.Play;
import play.mvc.Http.Context;
import play.mvc.Result;
import play.mvc.Security;
import play.libs.Json;

public class Secured extends Security.Authenticator {
  @Override
  public String getUsername(Context ctx) {
    String uid = ctx.session().get("uid");
    User user = User.findByUID(uid);
    if(user != null) {
      return user.email;
    } else {
      return null;
    }
  }

  @Override
  public Result onUnauthorized(Context ctx) {
    // When an unauthenticated client hits a `/debug/*` route while the MCP
    // surface is disabled, return 404 (not 401) so the endpoint looks like
    // it doesn't exist. Without this, Play 2.2's @Security.Authenticated
    // fires before Global.onRequest can short-circuit and leaks a 401 that
    // tells a scanner there's something to attack.
    if (ctx.request().path().startsWith("/debug/")) {
      Boolean mcpEnabled = Play.application().configuration().getBoolean("mcp.enabled");
      if (mcpEnabled == null || !mcpEnabled) {
        return notFound();
      }
    }
    ObjectNode result = Json.newObject();
    if (User.findRowCount() == 0) {
      result.put("status", "create-first-user");
    } else {
      result.put("status", "unauthorized");
    }
    result.put("message", "please login");
    return unauthorized(result);
  }
}
