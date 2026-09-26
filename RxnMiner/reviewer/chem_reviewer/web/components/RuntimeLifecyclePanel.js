import { createApp, h, onMounted, onUnmounted, reactive } from "vue";
import {
  applyExecutionDispatch,
  applyRuntimeEvent,
  createLifecycleState,
  isTerminalReviewProposalStatus,
  latestReplayableWorkspaceCommand,
  reconnectAfterAcceptedMessage,
  runtimeMessageBehavior,
  runtimeWorkspaceCommand,
  RuntimeEventStream,
  runtimeSourceLabel,
  summarizeRuntimeResult,
  summarizeToolActivity,
} from "/static/lib/runtime-events.js?v=20260802a";
import {
  pendingAfterReviewOutcomeSuccess,
  subscribeReviewOutcomes,
} from "/static/lib/review-outcome.js?v=20260727a";
import { dispatchWorkspaceCommand } from "/static/lib/workspace-command.js";
import { dispatchLiveEvidenceEvent } from "/static/lib/live-chemistry-evidence.js?v=20260731c";

function eventLabel(event) {
  const data = event.data && typeof event.data === "object" ? event.data : {};
  return data.phase || data.message || data.code || event.type;
}

function lifecycleDomIds(sessionId) {
  const suffix = String(sessionId).replace(/[^A-Za-z0-9_-]/g, "-");
  return {
    title: `runtime-lifecycle-title-${suffix}`,
    message: `runtime-message-${suffix}`,
    apiKey: `runtime-api-key-${suffix}`,
  };
}

export function runtimeConversationDisplay(message) {
  const text = typeof message?.text === "string" ? message.text.trim() : "";
  const skillMatch = text.match(/^\/skill:([^\s]+)/);
  const generatedTask = message?.role === "user" && Boolean(skillMatch) && text.length > 600;
  if (!generatedTask) return { collapsed: false, summary: "", text };
  const skill = skillMatch[1];
  const taskType = skill.includes("pdf") ? "PDF extraction"
    : skill.includes("reaction") ? "Figure extraction"
      : "Extraction";
  return {
    collapsed: true,
    summary: `${taskType} started · /skill:${skill}`,
    text,
  };
}

export const RuntimeLifecyclePanel = {
  name: "RuntimeLifecyclePanel",
  props: {
    sessionId: { type: String, required: true },
  },
  setup(props) {
    const domIds = lifecycleDomIds(props.sessionId);
    const state = reactive(createLifecycleState());
    const resultState = reactive({
      loading: false,
      error: "",
      summary: null,
      registering: false,
      registerError: "",
    });
    const compose = reactive({
      text: "",
      behavior: "prompt",
      sending: false,
      aborting: false,
      retrying: false,
      credentialSending: false,
      apiKey: "",
      error: "",
    });
    const reviewSync = reactive({
      pending: [],
      syncing: false,
      error: "",
    });
    let stream;
    let resultAbort;
    let historyAbort;
    let unsubscribeReviewOutcomes = () => {};
    let reviewOutcomeQueue = Promise.resolve();
    let nextReviewOutcomeOrder = 0;
    const reviewOutcomeOrders = new WeakMap();
    const canMessage = () => ["running", "ready", "dormant"].includes(state.status);
    const sealedTurn = () => ["review_ready", "partial"].includes(state.status);
    const terminalTurn = () => [
      "review_ready", "partial", "cancelled", "interrupted", "failed", "aborted", "blocked",
      "dormant",
    ].includes(state.status);
    const canStartLinkedTask = () => sealedTurn() && state.source.mode === "live_pi_sdk";
    const canCompose = () => canMessage() || canStartLinkedTask();
    const waitingForQueueCredential = () => (
      state.status === "awaiting_user_auth"
      && state.executionQueue.state === "blocked"
      && state.executionQueue.credentialRequired
      && state.auth.mode === "api_key"
    );
    const canAbort = () => state.executionQueue.state !== "cancelling" && (
      ["pending", "active", "blocked"].includes(state.executionQueue.state)
      || ["running", "ready"].includes(state.status)
    );
    const loadResult = async () => {
      if (resultState.loading || resultState.summary) return;
      resultState.loading = true;
      resultState.error = "";
      resultAbort = new AbortController();
      try {
        let response;
        for (let attempt = 0; attempt < 5; attempt += 1) {
          response = await fetch(
            `/api/runtime/sessions/${encodeURIComponent(props.sessionId)}/result`,
            { signal: resultAbort.signal },
          );
          if (response.status !== 409 || attempt === 4) break;
          await new Promise((resolve) => setTimeout(resolve, 150));
        }
        if (!response.ok) throw new Error(`Result request failed (HTTP ${response.status})`);
        const summary = summarizeRuntimeResult(await response.json());
        if (!summary) throw new Error("Runtime result contract is invalid or unsupported");
        resultState.summary = summary;
        if (
          state.source.mode === "sealed_replay"
          && state.auth.provider === "unknown"
          && summary.execution?.provider
        ) {
          // The replay's provider/model describe the immutable run that made
          // these artifacts. They do not imply a new authenticated live call.
          state.auth.provider = summary.execution.provider;
        }
        // Persisted events remain the truthful progress stream while work is
        // running.  Once a sealed PDF result exists, its immutable manifest is
        // authoritative for the final census; a missed/compacted discovery
        // event must not leave the UI saying "4 anchors" beside a 5-anchor
        // result.
        if (summary.contextMode === "paper" || summary.contextMode === "paper_si") {
          const metrics = Object.fromEntries(summary.metrics || []);
          if (Number.isSafeInteger(metrics["Main PDF pages"])) {
            state.pdf.active = true;
            state.pdf.totalPages = metrics["Main PDF pages"];
            state.pdf.completedPages = metrics["Main PDF pages"];
          }
          if (Number.isSafeInteger(metrics.Anchors)) state.pdf.anchors = metrics.Anchors;
        }
        if (summary.reviewable && !new URL(window.location.href).searchParams.has("job")) {
          void openVisualWorkspace({ automatic: true });
        }
      } catch (error) {
        if (error?.name !== "AbortError") {
          resultState.error = error?.message || "Runtime result is unavailable.";
        }
      } finally {
        resultState.loading = false;
      }
    };
    const sendMessage = async () => {
      const text = compose.text.trim();
      const previousStatus = state.status;
      const apiKeyRequired = state.status === "dormant" && state.auth.mode === "api_key";
      if (!text || compose.sending || !canCompose() || (apiKeyRequired && !compose.apiKey.trim())) return;
      compose.sending = true;
      compose.error = "";
      try {
        const apiKey = apiKeyRequired ? compose.apiKey : "";
        compose.apiKey = "";
        const response = await fetch(
          `/api/runtime/sessions/${encodeURIComponent(props.sessionId)}/messages`,
          {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({
              text,
              behavior: runtimeMessageBehavior(state, compose.behavior),
              ...(apiKey ? { apiKey } : {}),
            }),
          },
        );
        const body = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(body?.detail || body?.error?.message || `Message rejected (HTTP ${response.status})`);
        }
        compose.text = "";
        if (body?.continued && body?.session?.id) {
          const target = new URL(window.location.href);
          target.searchParams.set("runtime_session", body.session.id);
          if (typeof window.switchChemrxnRuntimeSession === "function") {
            window.history.replaceState(
              window.history.state,
              "",
              `${target.pathname}${target.search}${target.hash}`,
            );
            // Unmount this panel only after its accepted-message handler has
            // returned. History and the chemistry workspace remain mounted.
            setTimeout(() => {
              window.switchChemrxnRuntimeSession(body.session.id);
            }, 0);
          } else {
            window.location.assign(target.toString());
          }
        } else {
          reconnectAfterAcceptedMessage(previousStatus, body, stream);
        }
      } catch (error) {
        compose.error = error?.message || "Message could not be sent.";
      } finally {
        compose.sending = false;
      }
    };
    const abortAgent = async () => {
      if (compose.aborting) return;
      compose.aborting = true;
      compose.error = "";
      try {
        const response = await fetch(
          `/api/runtime/sessions/${encodeURIComponent(props.sessionId)}/abort`,
          { method: "POST" },
        );
        const body = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(body?.detail || body?.error?.message || `Abort rejected (HTTP ${response.status})`);
        }
      } catch (error) {
        compose.error = error?.message || "Agent could not be stopped.";
      } finally {
        compose.aborting = false;
      }
    };
    const resumeQueuedWithCredential = async () => {
      const apiKey = compose.apiKey.trim();
      if (!apiKey || compose.credentialSending || !waitingForQueueCredential()) return;
      compose.credentialSending = true;
      compose.error = "";
      // The password field is a one-shot handoff. Clear it before awaiting the
      // network so Vue, browser history and diagnostics cannot retain it.
      compose.apiKey = "";
      try {
        const response = await fetch(
          `/api/runtime/sessions/${encodeURIComponent(props.sessionId)}/credentials`,
          {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ apiKey }),
          },
        );
        const body = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(
            body?.detail
            || body?.error?.message
            || `Credential handoff rejected (HTTP ${response.status})`,
          );
        }
        if (!applyExecutionDispatch(state, body)) {
          throw new Error("Credential handoff returned an invalid dispatch state");
        }
      } catch (error) {
        compose.error = error?.message || "The queued extraction could not be resumed.";
      } finally {
        compose.credentialSending = false;
      }
    };
    const retryAgent = async () => {
      if (compose.retrying) return;
      compose.retrying = true;
      compose.error = "";
      try {
        const response = await fetch(
          `/api/runtime/sessions/${encodeURIComponent(props.sessionId)}/retry`,
          {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: "{}",
          },
        );
        const body = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(body?.detail || body?.error?.message || `Retry rejected (HTTP ${response.status})`);
        }
        if (!body?.session?.id) throw new Error("Retry returned no new session");
        const target = new URL(window.location.href);
        target.searchParams.set("runtime_session", body.session.id);
        window.location.assign(target.toString());
      } catch (error) {
        compose.error = error?.message || "A new extraction attempt could not be started.";
      } finally {
        compose.retrying = false;
      }
    };
    const retryEventStream = () => {
      state.error = null;
      stream?.connect();
    };
    const sameReviewOutcome = (left, right) => (
      left?.proposalId === right?.proposalId
      && left?.status === right?.status
      && left?.reviewedVersion === right?.reviewedVersion
      && left?.patchId === right?.patchId
      && left?.reason === right?.reason
    );
    const retainFailedReviewOutcome = (outcome) => {
      if (!reviewSync.pending.some((item) => sameReviewOutcome(item, outcome))) {
        reviewSync.pending.push(outcome);
      }
    };
    const persistReviewOutcome = (outcome) => {
      if (!reviewOutcomeOrders.has(outcome)) {
        reviewOutcomeOrders.set(outcome, ++nextReviewOutcomeOrder);
      }
      reviewOutcomeQueue = reviewOutcomeQueue
        .catch(() => undefined)
        .then(async () => {
          reviewSync.syncing = true;
          reviewSync.error = "";
          let lastError;
          for (let attempt = 0; attempt < 3; attempt += 1) {
            try {
              const response = await fetch(
                `/api/runtime/sessions/${encodeURIComponent(props.sessionId)}/review-outcomes`,
                {
                  method: "POST",
                  headers: { "content-type": "application/json" },
                  body: JSON.stringify(outcome),
                },
              );
              const body = await response.json().catch(() => ({}));
              if (!response.ok) {
                const error = new Error(
                  body?.detail
                  || body?.error?.message
                  || `Review outcome was not recorded (HTTP ${response.status})`,
                );
                error.retryable = response.status >= 500;
                throw error;
              }
              if (body?.event) applyRuntimeEvent(state, body.event);
              reviewSync.pending = pendingAfterReviewOutcomeSuccess(
                reviewSync.pending,
                outcome,
                (item) => reviewOutcomeOrders.get(item) || 0,
              );
              reviewSync.error = reviewSync.pending.length
                ? `${reviewSync.pending.length} reviewer status update(s) still need sync.`
                : "";
              return;
            } catch (error) {
              lastError = error;
              if (error?.retryable === false || attempt === 2) break;
              await new Promise((resolve) => setTimeout(resolve, 100 * (attempt + 1)));
            }
          }
          throw lastError;
        })
        .catch((error) => {
          retainFailedReviewOutcome(outcome);
          reviewSync.error = [
            "The reviewer action completed, but its conversation status was not persisted.",
            error?.message || "Retry the status sync.",
            `${reviewSync.pending.length} update(s) pending.`,
          ].join(" ");
        })
        .finally(() => {
          reviewSync.syncing = false;
        });
    };
    const retryReviewOutcome = () => {
      if (reviewSync.pending.length && !reviewSync.syncing) {
        for (const outcome of [...reviewSync.pending]) persistReviewOutcome(outcome);
      }
    };
    const openVisualWorkspace = async ({ automatic = false } = {}) => {
      if (resultState.registering) return;
      resultState.registering = true;
      resultState.registerError = "";
      try {
        const response = await fetch(
          `/api/runtime/sessions/${encodeURIComponent(props.sessionId)}/reviewer`,
          { method: "POST" },
        );
        const body = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(body?.detail || `Reviewer handoff failed (HTTP ${response.status})`);
        }
        if (!body.workspaceUrl) throw new Error("Reviewer handoff returned no workspace URL");
        const target = new URL(body.workspaceUrl, window.location.href);
        const targetJobs = target.searchParams.getAll("job");
        const targetSessions = target.searchParams.getAll("runtime_session");
        const targetJob = targetJobs[0];
        if (
          target.origin !== window.location.origin
          || target.pathname !== window.location.pathname
          || targetJobs.length !== 1
          || !targetJob
          || targetJob !== body.jobId
          || targetSessions.length !== 1
          || targetSessions[0] !== props.sessionId
        ) {
          throw new Error("Reviewer handoff returned an invalid same-origin workspace URL");
        }
        if (typeof window.loadJob === "function") {
          // Keep History, the Pi conversation, and the durable cursor mounted.
          // A full navigation reloaded the complete product shell (and Ketcher)
          // after every immutable Reviewer registration.
          const alreadyMounted =
            window.chemrxnWorkspaceMounted?.(targetJob) === true;
          if (!alreadyMounted) {
            const opened = await window.loadJob(targetJob, {
              requireMounted: true,
              silent: true,
            });
            if (opened !== true) {
              throw new Error("The visual workspace could not be mounted.");
            }
          }
          window.history.replaceState(
            window.history.state,
            "",
            `${target.pathname}${target.search}${target.hash}`,
          );
        } else if (automatic) {
          window.location.replace(target.toString());
        } else {
          window.location.assign(target.toString());
        }
      } catch (error) {
        resultState.registerError = error?.message || "Visual workspace could not be opened.";
      } finally {
        resultState.registering = false;
      }
    };
    const createLiveStream = () => new RuntimeEventStream({
        sessionId: props.sessionId,
        after: state.cursor,
        onConnection: (connection) => { state.connection = connection; },
        onEvent: (event) => {
          if (!applyRuntimeEvent(state, event)) return;
          dispatchLiveEvidenceEvent(event);
          const workspaceCommand = runtimeWorkspaceCommand(event);
          if (workspaceCommand) dispatchWorkspaceCommand(workspaceCommand);
          if (["review_ready", "partial"].includes(state.status)) void loadResult();
          if (terminalTurn()) {
            stream?.close();
            state.connection = sealedTurn() ? "sealed" : "complete";
          }
        },
        onProtocolError: (error) => {
          state.connection = "error";
          state.error = {
            code: error?.code || "RUNTIME_PROTOCOL_ERROR",
            message: error?.message || "The runtime event stream is invalid.",
          };
        },
      });
    const connectLiveStream = () => {
      if (!stream) stream = createLiveStream();
      stream.connect();
    };
    const hydrateHistory = async () => {
      historyAbort = new AbortController();
      try {
        const response = await fetch(
          `/api/runtime/sessions/${encodeURIComponent(props.sessionId)}/event-log?after=0`,
          { signal: historyAbort.signal },
        );
        const body = await response.json().catch(() => ({}));
        if (!response.ok || !Array.isArray(body.events)) {
          throw new Error(body?.detail || `Runtime history failed (HTTP ${response.status})`);
        }
        for (const event of body.events) {
          if (applyRuntimeEvent(state, event)) dispatchLiveEvidenceEvent(event);
        }
        // Sealed replays have persisted events and immutable artifacts, but no
        // execution-queue row by design.  Do not manufacture a failing HTTP
        // request for a queue that cannot exist; live sessions still hydrate
        // their durable dispatch state for cancel/retry/restart controls.
        if (state.source.mode !== "sealed_replay") {
          const dispatchResponse = await fetch(
            `/api/runtime/sessions/${encodeURIComponent(props.sessionId)}/dispatch`,
            { signal: historyAbort.signal },
          );
          if (dispatchResponse.ok) {
            applyExecutionDispatch(state, await dispatchResponse.json());
          } else if (dispatchResponse.status !== 404) {
            throw new Error(`Runtime dispatch failed (HTTP ${dispatchResponse.status})`);
          }
        }
        const restoredWorkspaceCommand = latestReplayableWorkspaceCommand(state);
        if (restoredWorkspaceCommand) dispatchWorkspaceCommand(restoredWorkspaceCommand);
        if (["review_ready", "partial"].includes(state.status)) await loadResult();
        stream = createLiveStream();
        if (terminalTurn()) {
          state.connection = sealedTurn() ? "sealed" : "complete";
          return;
        }
        connectLiveStream();
      } catch (error) {
        if (error?.name === "AbortError") return;
        state.connection = "error";
        state.error = {
          code: "RUNTIME_HISTORY_FAILED",
          message: error?.message || "Persisted runtime history could not be restored.",
        };
      }
    };
    onMounted(() => {
      unsubscribeReviewOutcomes = subscribeReviewOutcomes(
        persistReviewOutcome,
        window,
      );
      void hydrateHistory();
    });
    onUnmounted(() => {
      unsubscribeReviewOutcomes();
      stream?.close();
      historyAbort?.abort();
      resultAbort?.abort();
    });

    return () => h("section", {
      class: `runtime-lifecycle status-${state.status}`,
      "aria-labelledby": domIds.title,
      "aria-busy": String(
        ["connecting", "reconnecting"].includes(state.connection)
        || resultState.loading
        || resultState.registering
        || compose.sending
        || compose.aborting
        || compose.retrying
        || compose.credentialSending,
      ),
    }, [
      h("header", { class: "runtime-agent-header" }, [
        h("span", { class: "runtime-agent-avatar", "aria-hidden": "true" }, "π"),
        h("div", { class: "runtime-agent-identity" }, [
          h("h3", { id: domIds.title }, "Pi Agent"),
          h("span", { class: "runtime-agent-source" }, runtimeSourceLabel(state.source)),
        ]),
        h("span", {
          class: `runtime-connection connection-${state.connection}`,
          role: "status",
          "aria-live": "polite",
          "aria-atomic": "true",
        }, state.connection.replaceAll("_", " ")),
      ]),
      h("div", {
        class: "runtime-current",
        role: state.error ? "alert" : "status",
        "aria-live": state.error ? "assertive" : "polite",
        "aria-atomic": "true",
      }, [
        h("b", null, state.status.replaceAll("_", " ")),
        h("span", null, state.error?.message || state.message || "Waiting for the first persisted event."),
        h("small", null, `session ${props.sessionId} · cursor ${state.cursor}`),
        state.error?.code === "RUNTIME_CURSOR_GAP"
          ? h("button", {
            type: "button",
            class: "runtime-stream-retry",
            onClick: retryEventStream,
          }, "Retry event stream")
          : null,
      ]),
      h("dl", { class: "runtime-auth-summary", "aria-label": "Runtime authentication" }, [
        h("div", null, [
          h("dt", null, "Auth"),
          h("dd", null, state.auth.mode === "pi_auth"
            ? "Pi user profile" : state.auth.mode.replaceAll("_", " ")),
        ]),
        h("div", null, [
          h("dt", null, "Provider"),
          h("dd", null, state.auth.provider),
        ]),
        h("div", null, [
          h("dt", null, "State"),
          h("dd", { class: `auth-${state.auth.status}` },
            state.auth.status.replaceAll("_", " ")),
        ]),
      ]),
      state.authRequired
        ? h("p", { class: "runtime-auth-note" },
            waitingForQueueCredential()
              ? "This queued BYO-key run survived a restart, but its key did not. Re-enter it once to continue."
              : "Authentication must be completed by this user in their isolated Pi runtime.")
        : null,
      state.executionQueue.state !== "none"
        ? h("section", {
          class: `runtime-execution-queue queue-${state.executionQueue.state}`,
          "aria-label": "Local extraction queue",
          "aria-live": "polite",
        }, [
          h("div", null, [
            h("span", { class: "product-kicker" }, "Local task queue"),
            h("b", null, state.executionQueue.state === "pending"
              ? state.executionQueue.position
                ? `Position ${state.executionQueue.position}` : "Queued"
              : state.executionQueue.state.replaceAll("_", " ")),
          ]),
          state.executionQueue.concurrency
            ? h("small", null,
              `${state.executionQueue.ahead ?? 0} ahead · ${state.executionQueue.concurrency} local worker${state.executionQueue.concurrency === 1 ? "" : "s"}`)
            : null,
          state.executionQueue.message
            ? h("p", null, state.executionQueue.message)
            : null,
        ]) : null,
      waitingForQueueCredential()
        ? h("form", {
          class: "runtime-queue-credential",
          onSubmit: (event) => {
            event.preventDefault();
            void resumeQueuedWithCredential();
          },
        }, [
          h("label", { for: domIds.apiKey }, "API key for this queued run"),
          h("input", {
            id: domIds.apiKey,
            type: "password",
            value: compose.apiKey,
            autocomplete: "off",
            spellcheck: "false",
            placeholder: "Used once in memory · never saved",
            disabled: compose.credentialSending,
            onInput: (event) => { compose.apiKey = event.target.value; },
          }),
          h("button", {
            type: "submit",
            disabled: compose.credentialSending || !compose.apiKey.trim(),
          }, compose.credentialSending ? "Returning to queue…" : "Continue queued extraction"),
        ]) : null,
      state.figure.active
        ? h("section", {
          class: "runtime-figure-progress",
          "aria-label": "Figure extraction progress",
          "aria-live": "polite",
        }, [
          h("h4", null, "Figure extraction"),
          state.figure.total
            ? h("progress", {
              value: state.figure.completed,
              max: state.figure.total,
              "aria-label": "Figure extraction progress",
              "aria-valuetext": `${state.figure.completed} of ${state.figure.total} ${state.figure.unit}`,
            }) : null,
          h("dl", null, [
            h("div", null, [
              h("dt", null, state.figure.unit
                ? state.figure.unit.replaceAll("_", " ") : "Progress"),
              h("dd", null, state.figure.total
                ? `${state.figure.completed} / ${state.figure.total}` : "starting"),
            ]),
            h("div", null, [
              h("dt", null, "Candidates"),
              h("dd", null, String(state.figure.candidates)),
            ]),
            h("div", null, [
              h("dt", null, "MolParser detections"),
              h("dd", null, String(state.figure.detections)),
            ]),
            h("div", null, [
              h("dt", null, "Perception"),
              h("dd", null, state.figure.cacheStatus),
            ]),
          ]),
          h("small", null, state.figure.phase.replaceAll("_", " ").replaceAll(".", " · ")),
        ]) : null,
      state.pdf.active
        ? h("section", {
          class: "runtime-pdf-progress",
          "aria-label": "PDF extraction progress",
          "aria-live": "polite",
        }, [
          h("h4", null, "Document perception"),
          state.pdf.totalPages
            ? h("progress", {
              value: state.pdf.completedPages,
              max: state.pdf.totalPages,
              "aria-label": "PDF extraction page progress",
              "aria-valuetext": `${state.pdf.completedPages} of ${state.pdf.totalPages} pages`,
            }) : null,
          h("dl", null, [
            h("div", null, [
              h("dt", null, "Pages"),
              h("dd", null, state.pdf.totalPages
                ? `${state.pdf.completedPages} / ${state.pdf.totalPages}` : "validating"),
            ]),
            h("div", null, [
              h("dt", null, "Anchors"),
              h("dd", null, String(state.pdf.anchors)),
            ]),
            h("div", null, [
              h("dt", null, "MolParser detections"),
              // A sealed/cache-served replay has 0 live detections by design; a bare
              // "0" reads as a perception failure, so name the cache instead.
              h("dd", null, state.pdf.detections === 0 && state.pdf.cacheHits > 0
                ? "0 · served from cache"
                : String(state.pdf.detections)),
            ]),
            h("div", null, [
              h("dt", null, "Cache hits"),
              h("dd", null, String(state.pdf.cacheHits)),
            ]),
          ]),
          h("small", null, state.pdf.phase.replaceAll("_", " ").replaceAll(".", " · ")),
        ]) : null,
      state.conversation.length || state.streamingAssistant
        ? h("section", { class: "runtime-conversation", "aria-label": "Pi conversation" }, [
          h("h4", null, "Pi conversation"),
          ...state.conversation.map((message) => {
            const display = runtimeConversationDisplay(message);
            const proposalTerminal =
              isTerminalReviewProposalStatus(message.proposalStatus);
            const proposalStatus = message.proposalStatus === "applied" && message.reviewedVersion
              ? `Applied · reviewed-v${message.reviewedVersion}`
              : message.proposalStatus
                ? message.proposalStatus.replaceAll("_", " ")
                : "";
            return h("article", {
              key: message.id,
              class: `runtime-message message-${message.role}${display.collapsed ? " is-task-contract" : ""}`,
            }, [
              h("small", null, message.role === "user"
                ? display.collapsed ? "Extraction task" : `You · ${message.behavior.replaceAll("_", " ")}`
                : "Pi Agent"),
              display.collapsed
                ? h("details", { class: "runtime-task-contract" }, [
                  h("summary", null, display.summary),
                  h("p", null, display.text),
                ])
                : message.workspaceCommand
                  ? h("button", {
                    class: `runtime-workspace-citation${proposalTerminal ? " is-terminal" : ""}`,
                    type: "button",
                    disabled: proposalTerminal,
                    onClick: () => {
                      if (!proposalTerminal) dispatchWorkspaceCommand(message.workspaceCommand);
                    },
                  }, [
                    h("span", null, display.text),
                    message.proposalStatus
                      ? h("small", { class: `proposal-status status-${message.proposalStatus}` },
                        proposalStatus)
                      : h("small", null, "Open in chemistry workspace →"),
                  ])
                  : h("p", null, display.text),
            ]);
          }),
          state.streamingAssistant
            ? h("article", {
              class: "runtime-message message-assistant is-streaming",
              "aria-live": "polite",
            }, [
              h("small", null, "Pi Agent · streaming"),
              h("p", null, state.streamingAssistant),
            ])
            : null,
        ]) : null,
      resultState.summary?.execution
        ? h("section", {
          class: "runtime-execution-provenance",
          "aria-label": "Extraction execution provenance",
        }, [
          h("div", null, [
            h("span", { class: "product-kicker" }, "Extraction contract"),
            h("b", null, resultState.summary.execution.skill),
            resultState.summary.execution.skillVersion
              ? h("code", null, resultState.summary.execution.skillVersion.slice(0, 9)) : null,
          ]),
          h("small", null,
            `${resultState.summary.execution.provider} · ${resultState.summary.execution.model}`),
          h("p", null, resultState.summary.execution.evidenceMode === "sealed_tool_ledger"
            ? "Sealed tool ledger · call names and states only; no new model or MolParser call."
            : resultState.summary.execution.evidenceMode === "sealed_stage_ledger"
              ? "Sealed document stage ledger · reconstructed from immutable artifacts; not live execution."
              : "Captured live Pi execution events."),
        ]) : null,
      state.tools.length
        ? h("section", { class: "runtime-tools", "aria-label": "Chemistry tool activity" }, [
          h("h4", null, state.tools.some((tool) => tool.reconstructed)
            ? "Recorded tool ledger" : "Chemistry tools"),
          state.tools.some((tool) => tool.reconstructed)
            ? h("small", { class: "runtime-tools-disclosure" },
              state.toolHistoryTruncated
                ? `Reconstructed from the sealed run manifest. Showing only the most recent ${
                  state.tools.length
                } calls; arguments and outputs were not captured.`
                : "Reconstructed from the sealed run manifest. Arguments and outputs were not captured.")
            : state.toolHistoryTruncated
              ? h("small", { class: "runtime-tools-disclosure" },
                `Showing only the most recent ${state.tools.length} live tool calls.`)
              : null,
          h("ol", null, summarizeToolActivity(state.tools).map((tool) => h("li", {
            key: tool.name,
            class: `tool-${tool.status}`,
          }, [
            h("b", null, tool.name),
            h("span", null, `${tool.count}× · ${tool.status}`),
          ]))),
        ]) : null,
      state.toolArgumentProgress.active
        ? h("p", {
          class: "runtime-tool-argument-progress",
          role: "status",
          "aria-live": "polite",
        }, `Preparing a validated tool input · ${Math.ceil(
          state.toolArgumentProgress.bytes / 1024,
        )} KB · contents hidden from the event log`)
        : null,
      h("form", {
        class: "runtime-composer",
        onSubmit: (event) => {
          event.preventDefault();
          void sendMessage();
        },
      }, [
        h("label", { for: domIds.message }, "Message Pi Agent"),
        state.status === "dormant" && state.auth.mode === "api_key"
          ? h("div", { class: "runtime-credential-reentry" }, [
            h("label", { for: domIds.apiKey }, "API key required to reopen this local conversation"),
            h("input", {
              id: domIds.apiKey,
              type: "password",
              value: compose.apiKey,
              autocomplete: "off",
              spellcheck: "false",
              placeholder: "Key is used once in memory and never saved",
              disabled: compose.sending,
              onInput: (event) => { compose.apiKey = event.target.value; },
            }),
          ]) : null,
        h("textarea", {
          id: domIds.message,
          value: compose.text,
          rows: 3,
          maxlength: 20000,
          placeholder: canStartLinkedTask()
            ? "Start a linked Ask/Inspect task from these immutable inputs…"
            : "Ask for extraction, inspect evidence, or refine the current task…",
          disabled: compose.sending || !canCompose(),
          onInput: (event) => { compose.text = event.target.value; },
          onKeydown: (event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
              event.preventDefault();
              void sendMessage();
            }
          },
        }),
        h("div", { class: "runtime-composer-actions" }, [
          h("select", {
            value: compose.behavior,
            "aria-label": "Message behavior",
            disabled: canStartLinkedTask(),
            onChange: (event) => { compose.behavior = event.target.value; },
          }, [
            h("option", { value: "prompt" }, "New prompt"),
            h("option", { value: "steer" }, "Steer current turn"),
            h("option", { value: "follow_up" }, "Queue follow-up"),
          ]),
          h("button", {
            type: "submit",
            disabled: compose.sending || !compose.text.trim() || !canCompose()
              || (state.status === "dormant" && state.auth.mode === "api_key"
                && !compose.apiKey.trim()),
          }, compose.sending
            ? "Starting…"
            : canStartLinkedTask() ? "Start linked task" : "Send"),
          h("button", {
            type: "button",
            class: "runtime-abort",
            disabled: compose.aborting || !canAbort(),
            onClick: () => { void abortAgent(); },
          }, compose.aborting ? "Stopping…" : "Stop"),
          ["cancelled", "interrupted", "failed", "aborted"].includes(state.status)
            ? h("button", {
              type: "button",
              class: "runtime-retry",
              disabled: compose.retrying,
              onClick: () => { void retryAgent(); },
            }, compose.retrying ? "Starting retry…" : "Retry as new attempt")
            : null,
        ]),
        state.queue.steering || state.queue.followUp
          ? h("small", { class: "runtime-queue" },
            `${state.queue.steering} steer · ${state.queue.followUp} follow-up queued`)
          : null,
        canStartLinkedTask()
          ? h("small", { class: "runtime-queue" },
            "This extraction turn is sealed. Your message creates a linked task from the immutable original inputs; these artifacts remain unchanged.")
          : sealedTurn()
            ? h("small", { class: "runtime-queue" },
              "This is a read-only sealed preview. Start a live extraction before creating Ask/Inspect tasks.")
          : null,
        compose.error ? h("p", { class: "runtime-result-error", role: "alert" }, compose.error) : null,
        reviewSync.error ? h("div", {
          class: "runtime-review-sync-error",
          role: "alert",
        }, [
          h("span", null, reviewSync.error),
          h("button", {
            type: "button",
            disabled: reviewSync.syncing,
            onClick: retryReviewOutcome,
          }, reviewSync.syncing ? "Syncing…" : "Retry status sync"),
        ]) : null,
      ]),
      h("details", { class: "runtime-diagnostics" }, [
        h("summary", null, `Activity · ${state.events.length} persisted events`),
        h("ol", { class: "runtime-timeline", "aria-label": "Persisted runtime events" },
          state.events.slice(-20).map((event) => h("li", { key: event.seq }, [
            h("span", null, String(event.seq)),
            h("div", null, [
              h("b", null, eventLabel(event)),
              h("small", null, `${event.type} · ${event.at}`),
            ]),
          ]))),
      ]),
      resultState.loading
        ? h("p", { class: "runtime-result-loading", role: "status" }, "Loading persisted result…")
        : null,
      resultState.error
        ? h("p", { class: "runtime-result-error", role: "alert" }, resultState.error)
        : null,
      resultState.summary ? h("section", {
        class: "runtime-result",
        "aria-label": resultState.summary.title,
      }, [
        h("header", null, [
          h("div", null, [
            h("span", { class: "product-kicker" }, "Result"),
            h("h4", null, resultState.summary.title),
          ]),
          h("span", {
            class: `runtime-replay-badge ${resultState.summary.live ? "is-live" : ""}`,
          }, resultState.summary.live ? "live Pi result" : "sealed replay"),
        ]),
        h("p", { class: "runtime-result-source" },
          resultState.summary.live
            ? `${resultState.summary.sourceLabel} · ${resultState.summary.contextMode} · persisted live execution`
            : `${resultState.summary.fixture} · ${resultState.summary.contextMode} · reconstructed, not live execution`),
        h("dl", { class: "runtime-result-metrics" },
          resultState.summary.metrics.flatMap(([label, value]) => [
            h("div", { key: `${label}:term` }, [
              h("dt", null, label),
              h("dd", null, String(value)),
            ]),
          ])),
        resultState.summary.reactions.length
          ? h("ol", {
            class: "runtime-result-reactions",
            "aria-label": "Scrollable reaction result preview",
            tabindex: "0",
          },
            resultState.summary.reactions.map((reaction) => h("li", { key: reaction.id }, [
              h("span", null, reaction.label),
              h("small", null, reaction.outcome),
            ])))
          : null,
        resultState.summary.artifacts?.length
          ? h("nav", {
            class: "runtime-result-artifacts",
            "aria-label": "Persisted extraction artifacts",
          }, resultState.summary.artifacts.map((artifact) => h("a", {
            key: artifact.artifactId,
            href: `/api/runtime/sessions/${encodeURIComponent(props.sessionId)}/artifacts/${encodeURIComponent(artifact.artifactId)}`,
            target: "_blank",
            rel: "noopener",
          }, artifact.label)))
          : null,
        resultState.summary.reviewable
          ? h("button", {
            type: "button",
            class: "primary-action runtime-open-workspace",
            disabled: resultState.registering,
            onClick: () => { void openVisualWorkspace(); },
          }, resultState.registering ? "Preparing visual workspace…"
            : resultState.summary.contextMode === "figure"
              ? "Open visual reaction workspace"
              : "Open visual document workspace")
          : null,
        resultState.registerError
          ? h("p", { class: "runtime-result-error", role: "alert" }, resultState.registerError)
          : null,
        h("a", {
          class: "runtime-result-json",
          href: `/api/runtime/sessions/${encodeURIComponent(props.sessionId)}/result`,
          target: "_blank",
          rel: "noopener",
        }, "Open persisted result JSON"),
      ]) : null,
    ]);
  },
};

export function mountRuntimeLifecyclePanel(element, options = {}) {
  if (
    options.sessionId
    && element.__runtimeLifecycleApp
    && element.__runtimeLifecycleSessionId === options.sessionId
  ) {
    return element.__runtimeLifecycleApp;
  }
  element.__runtimeLifecycleApp?.unmount();
  if (!options.sessionId) {
    element.replaceChildren();
    element.__runtimeLifecycleApp = null;
    element.__runtimeLifecycleSessionId = null;
    return null;
  }
  const app = createApp(RuntimeLifecyclePanel, { sessionId: options.sessionId });
  app.mount(element);
  element.__runtimeLifecycleApp = app;
  element.__runtimeLifecycleSessionId = options.sessionId;
  return app;
}
