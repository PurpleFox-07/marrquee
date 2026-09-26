/*
 * Keeps a `list` field's own "open the guide" link in step with the
 * <select> beside it. `partials/app_questions.html` is included more than
 * once on the very same page (the Hub's choose-login banner, its install
 * pane, and the wizard's own question page), so this is scoped to the
 * closest `.question-step` rather than a plain page-wide querySelector,
 * which would only ever find the first one.
 *
 * The link's `href` starts out already correct on a no-JS page (the saved
 * company's own page, or the guide's index when nothing is chosen yet) -
 * this only ever copies a chosen `<option>`'s own `data-guide-url` onto it,
 * never composes one, and leaves the link untouched when that option
 * carries none.
 */
(function () {
  "use strict";

  document.addEventListener("change", function (event) {
    var select = event.target;
    if (!(select instanceof Element) || !select.hasAttribute("data-guide-select")) {
      return;
    }
    var step = select.closest(".question-step");
    if (!step) {
      return;
    }
    var link = step.querySelector('[data-role="question-guide"]');
    if (!link) {
      return;
    }
    var chosen = select.options[select.selectedIndex];
    var url = chosen ? chosen.getAttribute("data-guide-url") : null;
    if (url) {
      link.setAttribute("href", url);
    }
  });
})();
