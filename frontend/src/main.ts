import "./styles.css";
import "htmx.org";
import "idiomorph/htmx"; // registers hx-ext="morph" (Den Hurtige's feed; see ./feed)
import Alpine from "alpinejs";
import "./htmx-csrf"; // X-CSRFToken on every htmx request (site-wide; must precede any hx-post)
import "./charts"; // interactive charts on /intern/statistik/ (no-op elsewhere)
import "./imageupload"; // downscale room-inspection photo uploads client-side (no-op elsewhere)
import "./push"; // this device's push subscription widget, notifications/_device.html (no-op without one)
import "./live"; // the page's WebSocket to its live topic, if it declares one (no-op elsewhere)
import "./feed"; // Den Hurtige live-feed poll guard (no-op elsewhere)
import "./live-region"; // elements that re-draw themselves on a live signal (no-op elsewhere)
import "./bell"; // the top bar's notification bell, live (no-op without one)
import "./opslagstavle-live"; // opslagstavlen's board: live cards and the "nye opslag" banner
import "./opslagstavle"; // opslagstavlen Markdown toolbar + image upload (no-op elsewhere)
import "./events"; // begivenheder guest-list picker (no-op elsewhere)
import "./reparationer"; // reparationer kanban drag-and-drop (no-op elsewhere)
import "./mention"; // @-mention picker on every [data-mentionable] field (no-op without one)
import "./imageviewer"; // full-screen picture viewer: Arkiv, opslagstavlen, Den Hurtige
import "./arkiv"; // Arkiv direct-to-bucket upload + selection (no-op elsewhere)
import "./photo-album"; // photo album gallery viewer (no-op outside album pages)
import "./gahkflix"; // GAHKFlix: rows, uploader, editors and player (no-op outside body.flix)
import "./topbar"; // hide the sticky top bar on scroll-down (phones only)

// Alpine for small client-only interactions; HTMX (imported above) auto-wires hx-* attributes.
// NB: the ølkælder till (kiosk) is deliberately NOT an Alpine island — it runs on an iOS 10.3 iPad
// that cannot parse this bundle, so it ships self-contained ES5 in app/templates/oelkaelder/shop.html.
(window as unknown as { Alpine: typeof Alpine }).Alpine = Alpine;

Alpine.start();
