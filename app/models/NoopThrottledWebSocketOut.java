package models;

import com.fasterxml.jackson.databind.JsonNode;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CompletableFuture;

/**
 * A {@link ThrottledWebSocketOut} that doesn't actually send anything to a
 * WebSocket. Used by the MCP debug HTTP endpoints to drive Breadboard's actor
 * model without holding a real client connection. Capturing can be disabled
 * for the persistent debug admin; otherwise each message is kept in
 * {@link #captured()}, and a {@link CompletableFuture}
 * fires on the first message so HTTP handlers can synchronously wait for the
 * actor to do its work.
 */
public class NoopThrottledWebSocketOut extends ThrottledWebSocketOut {

  private final List<JsonNode> captured = new ArrayList<>();
  private final CompletableFuture<JsonNode> firstMessage = new CompletableFuture<>();
  private final boolean capture;

  public NoopThrottledWebSocketOut() {
    this(true);
  }

  public NoopThrottledWebSocketOut(boolean capture) {
    super(null, 0);
    this.capture = capture;
  }

  @Override
  public synchronized void write(JsonNode message) {
    if (!capture) {
      return;
    }
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
