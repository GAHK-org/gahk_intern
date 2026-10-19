/* Upload/insert toolbar for the CMS body fields (Django admin).
 *
 * Before this, putting a picture on the site meant committing the file to the repo, waiting for a
 * deploy, then hand-writing an <img> path. Now: pick a file, and the tag is written at the cursor.
 *
 * Plain ES5-ish DOM code on purpose — Django admin does not load the project's Vite bundle, so
 * there is no Alpine, no htmx and no build step behind this file.
 */
(function () {
  "use strict";

  // Endpoints live under the CmsImage admin (see CmsImageAdmin.get_urls), so they inherit its role
  // check. Derived from this page's own admin prefix rather than hardcoded, because the admin is
  // mounted at /django-admin/ here and that is a setting, not a constant.
  var adminRoot = window.location.pathname.split("/cms/")[0];
  var LIST_URL = adminRoot + "/cms/cmsimage/toolbar/list";
  var UPLOAD_URL = adminRoot + "/cms/cmsimage/toolbar/upload";

  var HTML_FIELDS = ["body", "description"];

  function csrf() {
    var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
    return input ? input.value : "";
  }

  function el(tag, props) {
    var node = document.createElement(tag);
    for (var key in props) {
      if (Object.prototype.hasOwnProperty.call(props, key)) node[key] = props[key];
    }
    return node;
  }

  /* Insert at the caret rather than appending: an editor adding a picture mid-article should not
     find it at the bottom of the page. */
  // NOTE: frontend/src/opslagstavle.ts has the same job for the board's Markdown toolbar, using
  // setRangeText (which keeps the native undo stack). The duplication is deliberate: the Django
  // admin does not load the Vite bundle, so sharing would mean building this file through Vite
  // for ten lines of selection arithmetic. Keep the two in mind together.
  function insertAtCursor(textarea, text) {
    var start = textarea.selectionStart;
    var end = textarea.selectionEnd;
    if (typeof start !== "number") {
      textarea.value += text;
      return;
    }
    textarea.value = textarea.value.slice(0, start) + text + textarea.value.slice(end);
    textarea.selectionStart = textarea.selectionEnd = start + text.length;
    textarea.focus();
  }

  function escapeAttr(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/"/g, "&quot;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function imgTag(image) {
    return '<img src="' + escapeAttr(image.url) + '" alt="' + escapeAttr(image.alt) + '">';
  }

  function build(textarea) {
    var bar = el("div");
    bar.style.cssText = "display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:0 0 6px";

    var picker = el("select");
    picker.style.maxWidth = "18rem";
    picker.appendChild(el("option", { value: "", textContent: "— vælg billede —" }));

    var insert = el("button", { type: "button", textContent: "Indsæt billede" });
    var file = el("input", { type: "file", accept: "image/jpeg,image/png,image/gif,image/webp,image/heic,image/heif,.heic,.heif" });
    file.style.display = "none";
    var upload = el("button", { type: "button", textContent: "📎 Upload og indsæt" });
    var status = el("span");
    status.style.cssText = "font-size:12px;color:#666";

    var known = [];

    function addOption(image) {
      known.push(image);
      picker.appendChild(el("option", { value: String(known.length - 1), textContent: image.label }));
    }

    fetch(LIST_URL, { credentials: "same-origin" })
      .then(function (r) {
        return r.ok ? r.json() : { images: [] };
      })
      .then(function (data) {
        (data.images || []).forEach(addOption);
      })
      .catch(function () {
        status.textContent = "Kunne ikke hente billedlisten.";
      });

    insert.addEventListener("click", function () {
      var choice = known[parseInt(picker.value, 10)];
      if (!choice) {
        status.textContent = "Vælg et billede først.";
        return;
      }
      insertAtCursor(textarea, imgTag(choice));
      status.textContent = "Indsat.";
    });

    upload.addEventListener("click", function () {
      file.click();
    });

    file.addEventListener("change", function () {
      if (!file.files || !file.files.length) return;
      var body = new FormData();
      body.append("file", file.files[0]);
      // Filename as a starting alt text: better than empty, and the editor can improve it in the
      // library. Screen-reader users get something either way.
      body.append("caption", file.files[0].name.replace(/\.[^.]+$/, ""));
      status.textContent = "Uploader…";
      upload.disabled = true;

      fetch(UPLOAD_URL, {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRFToken": csrf() },
        body: body,
      })
        .then(function (response) {
          return response.json().then(function (data) {
            if (!response.ok) throw new Error(data.error || "Upload mislykkedes.");
            return data;
          });
        })
        .then(function (image) {
          addOption(image);
          picker.value = String(known.length - 1);
          insertAtCursor(textarea, imgTag(image));
          status.textContent = "Uploadet og indsat.";
        })
        .catch(function (err) {
          status.textContent = err.message;
        })
        .finally(function () {
          upload.disabled = false;
          file.value = "";
        });
    });

    bar.appendChild(upload);
    bar.appendChild(picker);
    bar.appendChild(insert);
    bar.appendChild(file);
    bar.appendChild(status);
    textarea.parentNode.insertBefore(bar, textarea);
  }

  document.addEventListener("DOMContentLoaded", function () {
    HTML_FIELDS.forEach(function (name) {
      var textarea = document.querySelector('textarea[name="' + name + '"]');
      if (textarea) build(textarea);
    });
  });
})();
