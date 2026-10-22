/* Per-pipeline "Run" modal (Pipelines index).
 *
 * Each pipeline row carries a Run icon-button:
 *
 *   <button data-run-pipeline="<name>"
 *           data-input-open="true|false"
 *           data-output-open="true|false"> … </button>
 *
 * Clicking it opens the single shared modal (``[data-run-modal]``), points
 * the modal's form at ``/pipelines/<name>/run``, and reveals a folder /
 * schema navigator only for the side(s) the pipeline left *open* (an
 * ``open`` input or output has no bound address, so the operator must pick
 * storage for this run). A fully-bound pipeline shows no navigator — the
 * form submits bare and the pipeline runs against its configured storage.
 *
 * The navigators themselves are the ordinary storage pickers defined in
 * ``browse.js`` (``[data-storage-picker]``); this module only manages the
 * modal's visibility, per-side reveal, and reset between opens. The chosen
 * account + browsed folder land in the hidden ``<side>_connection`` /
 * ``<side>_location`` fields, which the ``/pipelines/<name>/run`` handler
 * resolves into an address and derives the batch id from.
 *
 * For ``explicit``-tables kinds (copy / move — flagged by
 * ``data-tables-required="true"``) the modal also reveals a ``tables``
 * text field, prefilled from the pipeline's stored tables
 * (``data-tables``, empty when the pipeline was created *open* on
 * tables). The field is disabled for every other kind so it never posts
 * a stray ``tables`` value; a blank required field blocks submit with an
 * inline message instead of the server's 400.
 *
 * Vanilla ES module, no build step.
 */
(function () {
  "use strict";

  var modal = document.querySelector("[data-run-modal]");
  if (!modal) return;

  var form = modal.querySelector("[data-run-modal-form]");
  var nameLabel = modal.querySelector("[data-run-modal-name]");
  var inputSide = modal.querySelector('[data-run-side="input"]');
  var outputSide = modal.querySelector('[data-run-side="output"]');
  var noneNote = modal.querySelector("[data-run-side-none]");
  var tablesSection = modal.querySelector("[data-run-tables]");
  var tablesInput = modal.querySelector("[data-run-tables-input]");
  var tablesError = modal.querySelector("[data-run-tables-error]");
  var tablesNote = modal.querySelector("[data-run-tables-note]");
  var tablesNoteAll = modal.querySelector("[data-run-tables-note-all]");

  // Whether the currently-open pipeline's source can expand ``all`` (an
  // empty tables field is then valid — it means "every table").
  var currentSupportsAll = false;

  // Reset a picker side back to its pristine state so a prior pipeline's
  // pick never leaks into the next open. Mirrors the initial server-render.
  function resetSide(side) {
    if (!side) return;
    var select = side.querySelector("[data-role=storage-select]");
    if (select) select.value = "";
    var crumbs = side.querySelector("[data-role=crumbs]");
    if (crumbs) crumbs.innerHTML = "";
    var listing = side.querySelector("[data-role=listing]");
    if (listing) {
      listing.innerHTML = '<p class="note">Pick a storage account to browse.</p>';
    }
    var chosen = side.querySelector("[data-role=chosen]");
    if (chosen) {
      chosen.hidden = true;
      chosen.innerHTML = "";
    }
    var err = side.querySelector("[data-role=picker-error]");
    if (err) {
      err.hidden = true;
      err.textContent = "";
    }
    var hidden = side.querySelectorAll('input[type="hidden"]');
    for (var i = 0; i < hidden.length; i++) hidden[i].value = "";
  }

  function openModal(btn) {
    var name = btn.getAttribute("data-run-pipeline") || "";
    var inputOpen = btn.getAttribute("data-input-open") === "true";
    var outputOpen = btn.getAttribute("data-output-open") === "true";
    var tablesRequired = btn.getAttribute("data-tables-required") === "true";
    var storedTables = btn.getAttribute("data-tables") || "";
    currentSupportsAll = btn.getAttribute("data-supports-all") === "true";

    if (nameLabel) nameLabel.textContent = name;
    if (form) form.action = "/pipelines/" + encodeURIComponent(name) + "/run";

    resetSide(inputSide);
    resetSide(outputSide);
    if (inputSide) inputSide.hidden = !inputOpen;
    if (outputSide) outputSide.hidden = !outputOpen;
    if (noneNote) noneNote.hidden = inputOpen || outputOpen;

    // Tables section: shown only for explicit-tables kinds (copy / move),
    // prefilled from the pipeline's stored tables (empty when the pipeline
    // was created *open* on tables). Disabled + cleared for every other
    // kind so it never posts a stray ``tables`` field.
    if (tablesError) {
      tablesError.hidden = true;
      tablesError.textContent = "";
    }
    if (tablesSection) tablesSection.hidden = !tablesRequired;
    if (tablesInput) {
      tablesInput.disabled = !tablesRequired;
      tablesInput.value = tablesRequired ? storedTables : "";
    }
    // Swap the explanatory note: sources that can enumerate their own
    // tables (e.g. Access) accept ``all`` / an empty field.
    if (tablesNote) tablesNote.hidden = currentSupportsAll;
    if (tablesNoteAll) tablesNoteAll.hidden = !currentSupportsAll;

    modal.hidden = false;
    document.body.classList.add("modal-open");
  }

  function closeModal() {
    modal.hidden = true;
    document.body.classList.remove("modal-open");
  }

  var runButtons = document.querySelectorAll("[data-run-pipeline]");
  for (var i = 0; i < runButtons.length; i++) {
    runButtons[i].addEventListener("click", function (evt) {
      evt.preventDefault();
      openModal(evt.currentTarget);
    });
  }

  // Block submit when a copy / move run has no tables (the server would
  // reject it with a 400 — catch it client-side for a friendlier message).
  if (form) {
    form.addEventListener("submit", function (evt) {
      if (
        tablesSection &&
        !tablesSection.hidden &&
        tablesInput &&
        !tablesInput.value.trim() &&
        !currentSupportsAll
      ) {
        evt.preventDefault();
        if (tablesError) {
          tablesError.textContent =
            "Enter at least one table \u2014 copy / move cannot run with " +
            "an empty (or `all`) tables list.";
          tablesError.hidden = false;
        }
        tablesInput.focus();
      }
    });
  }

  var closers = modal.querySelectorAll("[data-run-modal-close]");
  for (var j = 0; j < closers.length; j++) {
    closers[j].addEventListener("click", function (evt) {
      evt.preventDefault();
      closeModal();
    });
  }

  // Click on the dimmed backdrop (outside the panel) closes the modal.
  modal.addEventListener("click", function (evt) {
    if (evt.target === modal) closeModal();
  });

  document.addEventListener("keydown", function (evt) {
    if (evt.key === "Escape" && !modal.hidden) closeModal();
  });
})();
