// Hjemmeside (the gahk.dk editor, templates/hjemmeside/): its code is fetched only on its own pages,
// and the body editor (Tiptap, the bulk of it) only where there is a body to edit. The rest of the
// intranet never downloads either.
//
// Both dynamic imports live HERE, in the main bundle, on purpose. app.js is a classic script, and the
// lazily loaded chunks are ES modules: a chunk that imported anything back from app.js (a shared
// helper, Vite's preload function) would load app.js a second time, as a module, and start Alpine and
// htmx twice. So the chunks import nothing from the main bundle; what they need from it (the CSRF
// token, the editor loader) is handed to them.
import { csrfToken } from "./htmx-csrf";

const root = document.querySelector<HTMLElement>("[data-hjemmeside]");
if (root) {
  void import("./hjemmeside").then((hjemmeside) =>
    hjemmeside.start(root, { csrfToken, loadEditor: () => import("./hjemmeside-editor") }),
  );
}
