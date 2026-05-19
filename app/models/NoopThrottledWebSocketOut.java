package models;

import com.fasterxml.jackson.databind.JsonNode;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CompletableFuture;

/**
 * A {@link ThrottledWebSocketOut} that doesn't actually send anything to a
 * WebSocket. Used by the MCP debug HTTP endpoints to drive Breadboard's actor
 * model without holding a real client connection. Each message the actor would
 * have sent is captured in {@link #captured()}; a {@link CompletableFuture}
 * fires on the first message so HTTP handlers can synchronously wait for the
 * actor to do its work.
 */
public class NoopThrottledWebSocketOut extends ThrottledWebSocketOut {

  private final List<JsonNode> captured = new ArrayList<>();
  private final CompletableFuture<JsonNode> firstMessage = new CompletableFuture<>();

  public NoopThrottledWebSocketOut() {
    super(null, 0);
  }

  @Override
  public synchronized void write(JsonNode message) {
    captured.add(message);
    if (!firstMessage.isDone()) {
      firstMessage.complete(message);
    }
  }

  @Override
  public void close() {
    /* no-op */
  }

  public synchronized List<JsonNode> captured() {
    return new ArrayList<>(captured);
  }

  public CompletableFuture<JsonNode> firstMessage() {
    return firstMessage;
  }
}
