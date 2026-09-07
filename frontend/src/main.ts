import "./styles.css";
import "htmx.org";
import "idiomorph/htmx"; // registers hx-ext="morph" (Den Hurtige's feed; see ./feed)
import Alpine from "alpinejs";
import "./htmx-csrf"; // X-CSRFToken on every htmx request (site-wide; must precede any hx-post)
import "./charts"; // interactive charts on /intern/statistik/ (no-op elsewhere)
import "./imageupload"; // downscale room-inspection photo uploads client-side (no-op elsewhere)
import "./push"; // Den Hurtige push-notification subscribe button (no-op elsewhere)
import "./feed"; // Den Hurtige live-feed poll guard (no-op elsewhere)
import "./opslagstavle"; // opslagstavlen Markdown toolbar + image upload (no-op elsewhere)
import "./events"; // begivenheder guest-list picker (no-op elsewhere)
import "./reparationer"; // reparationer kanban drag-and-drop (no-op elsewhere)
import "./arkiv"; // Arkiv direct-to-bucket upload (no-op elsewhere)

// Alpine for small client-only interactions; HTMX (imported above) auto-wires hx-* attributes.
// NB: the ølkælder till (kiosk) is deliberately NOT an Alpine island — it runs on an iOS 10.3 iPad
// that cannot parse this bundle, so it ships self-contained ES5 in app/templates/oelkaelder/shop.html.
(window as unknown as { Alpine: typeof Alpine }).Alpine = Alpine;

Alpine.start();
