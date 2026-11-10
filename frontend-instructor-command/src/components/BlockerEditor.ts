import type { BlockerKind, BlockerMutation } from "../api/types.js";
import { escapeHtml } from "../lib/html.js";

export type BlockerEditorModel = {
  mode: "create" | "edit";
  blockerId: string | null;
  revision: number | null;
  idempotencyKey: string;
  draft: BlockerMutation;
  initialDraft: BlockerMutation;
  pending: boolean;
  error: string;
  deleteConfirm: boolean;
  discardConfirm: boolean;
};

const blockerKinds: Array<{ value: BlockerKind; label: string; hint: string }> = [
  { value: "break", label: "Break", hint: "Adds recovery time after it" },
  { value: "personal", label: "Personal", hint: "Can include a travel location" },
  { value: "unavailable", label: "Unavailable", hint: "Blocks time only" },
];

export function renderBlockerEditor(model: BlockerEditorModel): string {
  const heading = model.mode === "create" ? "Add schedule event" : `Edit ${model.draft.title}`;
  return `
    <div class="blocker-modal-backdrop" data-blocker-backdrop>
      <section
        class="blocker-editor-sheet"
        role="dialog"
        aria-modal="true"
        aria-labelledby="blocker-editor-title"
        aria-describedby="blocker-editor-description"
        tabindex="-1"
        data-blocker-dialog
      >
        ${model.deleteConfirm
          ? renderDeleteConfirmation(model)
          : model.discardConfirm
            ? renderDiscardConfirmation(model)
            : `
          <header class="blocker-editor-heading">
            <div>
              <p class="eyebrow">Protected schedule</p>
              <h2 id="blocker-editor-title">${escapeHtml(heading)}</h2>
              <p id="blocker-editor-description">These events are excluded from every safe-time search.</p>
            </div>
            <button class="blocker-modal-close" type="button" data-blocker-close aria-label="Close schedule event editor">×</button>
          </header>
          <form class="blocker-editor-form" data-blocker-form>
            <fieldset class="blocker-kind-picker" ${model.pending ? "disabled" : ""}>
              <legend>Event type</legend>
              ${blockerKinds.map((kind) => `
                <label class="blocker-kind-option ${model.draft.kind === kind.value ? "selected" : ""}">
                  <input
                    type="radio"
                    name="kind"
                    value="${kind.value}"
                    ${model.draft.kind === kind.value ? "checked" : ""}
                  />
                  <strong>${escapeHtml(kind.label)}</strong>
                  <span>${escapeHtml(kind.hint)}</span>
                </label>
              `).join("")}
            </fieldset>
            <label class="blocker-field">
              <span>Title</span>
              <input name="title" value="${escapeHtml(model.draft.title)}" maxlength="160" autocomplete="off" required ${model.pending ? "disabled" : ""} />
            </label>
            <div class="blocker-date-time-grid">
              <label class="blocker-field blocker-date-field">
                <span>Date</span>
                <input name="date" type="date" value="${escapeHtml(model.draft.date)}" required ${model.pending ? "disabled" : ""} />
              </label>
              <label class="blocker-field">
                <span>Starts</span>
                <input name="start_time" type="time" value="${escapeHtml(model.draft.start_time)}" required ${model.pending ? "disabled" : ""} />
              </label>
              <label class="blocker-field">
                <span>Ends</span>
                <input name="end_time" type="time" value="${escapeHtml(model.draft.end_time)}" required ${model.pending ? "disabled" : ""} />
              </label>
            </div>
            ${model.draft.kind === "personal" ? `
              <label class="blocker-field">
                <span>Location <small>Optional</small></span>
                <input
                  name="location_address"
                  value="${escapeHtml(model.draft.location_address ?? "")}"
                  maxlength="300"
                  autocomplete="street-address"
                  placeholder="Address for travel calculations"
                  ${model.pending ? "disabled" : ""}
                />
              </label>
            ` : ""}
            <p class="blocker-editor-status ${model.error ? "error" : ""}" aria-live="assertive" data-blocker-status tabindex="-1">
              ${escapeHtml(model.pending ? "Saving schedule event…" : model.error)}
            </p>
            <div class="blocker-editor-actions">
              ${model.mode === "edit" ? `<button class="blocker-remove-action" type="button" data-blocker-remove ${model.pending ? "disabled" : ""}>Remove event</button>` : ""}
              <button class="primary-action" type="submit" ${model.pending ? "disabled" : ""}>
                ${model.pending ? "Saving…" : model.mode === "create" ? "Add to schedule" : "Save changes"}
              </button>
            </div>
          </form>
        `}
      </section>
    </div>
  `;
}

function renderDeleteConfirmation(model: BlockerEditorModel): string {
  const persisted = model.initialDraft;
  return `
    <div class="blocker-delete-confirmation">
      <div class="blocker-delete-mark" aria-hidden="true">!</div>
      <p class="eyebrow">Remove schedule event</p>
      <h2 id="blocker-editor-title">Remove ${escapeHtml(persisted.title)}?</h2>
      <p id="blocker-editor-description">
        ${escapeHtml(persisted.date)} at ${escapeHtml(persisted.start_time)}-${escapeHtml(persisted.end_time)} will become available for scheduling again.
      </p>
      <p class="blocker-editor-status ${model.error ? "error" : ""}" aria-live="assertive" data-blocker-status tabindex="-1">
        ${escapeHtml(model.pending ? "Removing schedule event…" : model.error)}
      </p>
      <div class="blocker-confirm-actions">
        <button class="secondary-action" type="button" data-blocker-keep ${model.pending ? "disabled" : ""}>Keep event</button>
        <button class="danger-action" type="button" data-blocker-confirm-remove ${model.pending ? "disabled" : ""}>
          ${model.pending ? "Removing…" : "Remove event"}
        </button>
      </div>
    </div>
  `;
}

function renderDiscardConfirmation(model: BlockerEditorModel): string {
  return `
    <div class="blocker-delete-confirmation blocker-discard-confirmation">
      <div class="blocker-discard-mark" aria-hidden="true">!</div>
      <p class="eyebrow">Unsaved schedule event</p>
      <h2 id="blocker-editor-title">Discard changes to ${escapeHtml(model.draft.title || "this event")}?</h2>
      <p id="blocker-editor-description">
        Your edits have not been saved. Keep editing to preserve everything currently in the form.
      </p>
      <div class="blocker-confirm-actions">
        <button class="secondary-action" type="button" data-blocker-discard-keep>Keep editing</button>
        <button class="danger-action" type="button" data-blocker-confirm-discard>Discard changes</button>
      </div>
    </div>
  `;
}
