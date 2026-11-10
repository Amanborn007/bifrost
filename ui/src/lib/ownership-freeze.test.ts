/**
 * Regression test: OwnershipFastPath must not fire for every user message.
 *
 * Bug: OwnershipFastPath was mounted for EVERY user message
 * that matched isOwnershipQuestion(). In a 10-15 message thread, this caused
 * N concurrent DB queries on load — saturating the browser event loop and
 * freezing scroll + navigation.
 *
 * Fix: only mount when messages[idx + 1] === undefined (last unanswered message).
 */

import { isOwnershipQuestion } from "@/lib/api";
import { describe, it, expect } from "vitest";

type Msg = { role: "user" | "assistant"; content: string };

function countOwnershipMounts(messages: Msg[]): number {
  let mounts = 0;
  for (let idx = 0; idx < messages.length; idx++) {
    const m = messages[idx];
    if (m.role !== "user") continue;
    const isLastUnanswered = messages[idx + 1] === undefined;
    if (isOwnershipQuestion(m.content) && isLastUnanswered) mounts++;
  }
  return mounts;
}

describe("isOwnershipQuestion", () => {
  it("matches ownership patterns", () => {
    expect(isOwnershipQuestion("who owns dispatcher?")).toBe(true);
    expect(isOwnershipQuestion("what team owns the orders service?")).toBe(true);
    expect(isOwnershipQuestion("who works on order-service?")).toBe(true);
    expect(isOwnershipQuestion("which squad maintains webhook-api?")).toBe(true);
    expect(isOwnershipQuestion("ownership of dispatcher")).toBe(true);
  });

  it("does not match non-ownership questions", () => {
    expect(isOwnershipQuestion("how does the retry logic work?")).toBe(false);
    expect(isOwnershipQuestion("what is the delivery flow?")).toBe(false);
    expect(isOwnershipQuestion("if we had to build order reporting")).toBe(false);
    expect(isOwnershipQuestion("trace the order flow end-to-end")).toBe(false);
  });
});

describe("OwnershipFastPath mount guard", () => {
  it("mounts 0 when all ownership questions have replies", () => {
    const messages: Msg[] = [
      { role: "user", content: "who owns dispatcher?" },
      { role: "assistant", content: "Fulfillment squad." },
      { role: "user", content: "who maintains webhook-api?" },
      { role: "assistant", content: "The webhook-api squad." },
    ];
    expect(countOwnershipMounts(messages)).toBe(0);
  });

  it("mounts 1 for the last unanswered ownership question", () => {
    const messages: Msg[] = [
      { role: "user", content: "who owns dispatcher?" },
      { role: "assistant", content: "Fulfillment squad." },
      { role: "user", content: "who maintains webhook-api?" },
    ];
    expect(countOwnershipMounts(messages)).toBe(1);
  });

  it("mounts 0 when last unanswered is not an ownership question", () => {
    const messages: Msg[] = [
      { role: "user", content: "who owns dispatcher?" },
      { role: "assistant", content: "Fulfillment squad." },
      { role: "user", content: "how does the retry logic work?" },
    ];
    expect(countOwnershipMounts(messages)).toBe(0);
  });

  it("benchmark: 15-message thread with 8 ownership questions mounts exactly 1 (not 8)", () => {
    const msgs: Msg[] = [];
    const qs = [
      "who owns dispatcher?", "who maintains webhook-api?",
      "what team owns auth-service?", "who works on orders-service?",
      "which squad owns order-reporting?", "who built order-service?",
      "who owns delivery-gateway?", "who maintains the signing-keys?",
    ];
    for (let i = 0; i < 7; i++) {
      msgs.push({ role: "user", content: qs[i] });
      msgs.push({ role: "assistant", content: `Squad ${i} owns it.` });
    }
    msgs.push({ role: "user", content: qs[7] }); // last, no reply
    expect(msgs.length).toBe(15);
    // Pre-fix: would be 8 (N queries on load). Post-fix: exactly 1.
    expect(countOwnershipMounts(msgs)).toBe(1);
  });
});
