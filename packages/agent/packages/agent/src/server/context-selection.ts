import type { ContentBlock, PromptResponse } from "@agentclientprotocol/sdk";
import type { PostHogAPIClient } from "../posthog-api";
import { hiddenTextBlock } from "./cloud-prompt";

function isHidden(block: ContentBlock): boolean {
  const ui = block._meta?.ui;
  return (
    typeof ui === "object" &&
    ui !== null &&
    "hidden" in ui &&
    ui.hidden === true
  );
}

function text(prompt: ContentBlock[]): string {
  return prompt
    .flatMap((block) => (block.type === "text" ? [block.text] : []))
    .join("\n");
}

function tail(value: string, limit: number): string {
  return Array.from(value).slice(-limit).join("");
}

/** One instance per cloud process. Only actual human turns prepare context. */
export class ContextSelection {
  enabled = false;
  private history = "";
  private historyRunId: string | undefined;

  constructor(
    private readonly api: PostHogAPIClient,
    private readonly report: (
      event: Record<string, unknown>,
    ) => void = () => {},
    private readonly runtimeVersion = "unknown",
  ) {}

  resetHistory(runId: string, history = ""): void {
    this.historyRunId = runId;
    this.history = tail(history, 12_000);
  }

  async dispatch(
    runId: string,
    messageId: string | undefined,
    prompt: ContentBlock[],
    send: (blocks: ContentBlock[]) => Promise<PromptResponse>,
    humanPrompt = prompt,
  ): Promise<PromptResponse> {
    if (!this.enabled || !messageId) return send(prompt);
    const userText = text(humanPrompt.filter((block) => !isHidden(block)));
    if (userText.trimStart().startsWith("/")) return send(prompt);
    const submitted = await this.preparePrompt(
      runId,
      messageId,
      prompt,
      userText,
      text(humanPrompt.filter(isHidden)),
    );
    const result = await send(submitted);
    if (result.stopReason !== "cancelled") this.recordUser(runId, userText);
    return result;
  }

  private async preparePrompt(
    runId: string,
    messageId: string,
    prompt: ContentBlock[],
    userText: string,
    restoredHistory: string,
  ): Promise<ContentBlock[]> {
    let prepared:
      | Awaited<ReturnType<PostHogAPIClient["prepareContextSelection"]>>
      | undefined;
    if (this.historyRunId !== runId) {
      this.resetHistory(runId);
    }
    if (!this.history) this.history = tail(restoredHistory, 12_000);
    const history = this.history;
    try {
      prepared = await this.api.prepareContextSelection({
        run_id: runId,
        message_id: messageId,
        prompt: tail(userText, 20_000),
        history,
        runtime_version: this.runtimeVersion,
      });
    } catch {
      this.report({
        event: "prepare_failed",
        run_id: runId,
        message_id: messageId,
      });
    }
    return prepared?.context
      ? [...prompt, hiddenTextBlock(prepared.context)]
      : prompt;
  }

  private recordUser(runId: string, text: string): void {
    if (!this.enabled || this.historyRunId !== runId) return;
    this.history = tail(`${this.history}\nUser: ${text}`, 12_000);
  }

  recordAssistant(runId: string, text: string): void {
    if (!this.enabled || this.historyRunId !== runId) return;
    this.history = tail(`${this.history}\nAssistant: ${text}`, 12_000);
  }
}
