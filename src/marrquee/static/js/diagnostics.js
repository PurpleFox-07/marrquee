/*
 * Progressive enhancement for the Diagnostics page's Copy button. Every
 * word this script shows comes from a data- attribute on the button - it
 * never composes a sentence of its own. If this file fails to load, or
 * JavaScript is off, the button stays hidden and the text it would copy is
 * still visible and selectable by hand.
 */
(function () {
  "use strict";

  var source = document.querySelector("[data-copy-source]");
  var button = document.querySelector("[data-copy-button]");
  if (!source || !button) {
    return;
  }

  var status = document.querySelector("[data-copy-status]");

  button.hidden = false;
  button.addEventListener("click", copyLastProblem);

  // Three steps, tried in order: the modern clipboard API only exists on a
  // secure context (https or localhost), so the owner's plain
  // http://<NAS-IP>:7788 falls straight through to the older selection-based
  // copy. If even that is blocked, the text is left selected and the button
  // explains how to copy it by hand.
  function copyLastProblem() {
    if (window.isSecureContext && navigator.clipboard) {
      navigator.clipboard.writeText(source.value).then(reportCopied, fallbackCopy);
      return;
    }
    fallbackCopy();
  }

  function fallbackCopy() {
    source.focus();
    source.select();

    var copied = false;
    try {
      copied = document.execCommand("copy");
    } catch (error) {
      copied = false;
    }

    if (copied) {
      reportCopied();
    } else {
      reportBlocked();
    }
  }

  function reportCopied() {
    setStatus(button.dataset.copiedText || "", "copied");
  }

  function reportBlocked() {
    setStatus(button.dataset.blockedText || "", "blocked");
  }

  function setStatus(text, state) {
    if (!status) {
      return;
    }
    status.textContent = text;
    status.setAttribute("data-copy-status", state);
  }
})();
