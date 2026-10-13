/* Live preview of a CMS page's address, under the section picker (Django admin).
 *
 * An editor once renamed /faciliteter/kokken to faciliteter-kokken, saved, and the page fell out of
 * its section sidebar — invisible, and impossible to type back because "/" was rejected. The server
 * now composes the address from a section plus a final segment (cms.admin.PageAdminForm) and keeps
 * the old one redirecting (cms.services). This file's whole job is to make that visible *before*
 * the save: what the address will become, and that the old one will still work.
 *
 * Purely advisory. It never blocks submit and never writes to the hidden slug field — the server
 * composes the value, so a mismatch here could only mislead, never corrupt.
 *
 * Plain ES5-ish DOM code on purpose — Django admin does not load the project's Vite bundle, so
 * there is no Alpine, no htmx and no build step behind this file.
 */
(function () {
  "use strict";

  function onReady(fn) {
    if (document.readyState !== "loading") {
      fn();
    } else {
      document.addEventListener("DOMContentLoaded", fn);
    }
  }

  /* Mirrors cms.paths.normalize_segment: trim, lowercase, spaces to hyphens. Deliberately does NOT
   * strip illegal characters — the preview must show what the editor actually typed so the server's
   * error message makes sense, rather than quietly previewing a different address. */
  function normalizeSegment(value) {
    return value.trim().toLowerCase().replace(/\s+/g, "-").replace(/^\/+|\/+$/g, "");
  }

  function compose(parent, segment) {
    if (!segment) return "";
    return parent ? parent + "/" + segment : segment;
  }

  onReady(function () {
    var segmentField = document.querySelector('[name="path_segment"]');
    var parentField = document.querySelector('[name="path_parent"]');
    if (!segmentField || !parentField) return; // not the page form; nothing to do

    var row = segmentField.closest(".form-row") || segmentField.parentNode;
    var hint = document.createElement("div");
    hint.className = "help";
    hint.style.marginTop = "4px";
    row.appendChild(hint);

    var originalPath = compose(parentField.value, normalizeSegment(segmentField.value));

    /* Built as nodes rather than an HTML string: the segment is whatever the editor just typed, and
     * innerHTML would make this field a script-injection sink on the admin's own page. */
    function line(before, strong, after) {
      var div = document.createElement("div");
      if (before) div.appendChild(document.createTextNode(before));
      if (strong) {
        var em = document.createElement("strong");
        em.textContent = strong;
        div.appendChild(em);
      }
      if (after) div.appendChild(document.createTextNode(after));
      return div;
    }

    function render() {
      var path = compose(parentField.value, normalizeSegment(segmentField.value));
      hint.textContent = "";

      if (path) {
        hint.appendChild(line("Adressen bliver: ", "/" + path, ""));
      } else {
        hint.appendChild(
          line("Siden får ", "ingen offentlig adresse", " og kan ikke åbnes på sitet.")
        );
      }

      // Only once the address actually differs from what was loaded — saying this on every page
      // view would train editors to ignore it.
      if (originalPath && path && path !== originalPath) {
        hint.appendChild(
          line(
            "Den gamle adresse ",
            "/" + originalPath,
            " sender automatisk videre til den nye, så links og bogmærker bliver ved at virke."
          )
        );
      }
    }

    segmentField.addEventListener("input", render);
    parentField.addEventListener("change", render);
    render();
  });
})();
