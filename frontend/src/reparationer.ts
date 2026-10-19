// Reparationer: drag a card from one column of the kanban board to another. No-op on every page
// without a [data-kanban] board, and on the board itself for anyone who may not move tickets
// (data-can-move="0" — see reparationer.views.MOVE_ROLES).
//
// Pointer events rather than HTML5 drag-and-drop: `dragstart` never fires on a touchscreen, and
// this board is read on a phone at least as often as on a laptop. One code path drives mouse, pen
// and finger, which is also why the touch case has a long-press: a finger that starts moving
// straight away is scrolling the column, and hijacking that would make the board unreadable on a
// phone to buy a gesture nobody asked for there. A mouse has no such ambiguity — it drags as soon
// as it has moved past the slop threshold.
//
// The card is never the thing that follows the pointer. A clone does, `position: fixed` and
// `pointer-events: none`, while the real card stays in the DOM dimmed in its old column: the
// columns keep their layout (nothing reflows under the pointer mid-drag), the drop target under the
// cursor is findable with elementFromPoint, and putting a refused card back is doing nothing at all
// rather than restoring a position we would have had to remember.
//
// The server is the authority on every move. The drop is applied optimistically because the board
// should feel like moving a magnet on a fridge, but a refusal (a Vicevært aiming at a manager-only
// column, a lost session, a dead network) puts the card back where it came from and says why —
// see reparationer.views._move_response, which answers this fetch in JSON and every other caller
// with a redirect.

const root = document.querySelector<HTMLElement>("[data-kanban]");

if (root && root.dataset.canMove === "1") {
  const canManage = root.dataset.canManage === "1";
  const csrf = root.querySelector<HTMLInputElement>('input[name="csrfmiddlewaretoken"]')?.value ?? "";
  const note = document.querySelector<HTMLElement>("[data-kanban-message]");

  // How far a mouse must travel before this is a drag and not a click on the card's title link,
  // and how long a finger must rest before it is a drag and not a scroll.
  const SLOP_PX = 6;
  const LONG_PRESS_MS = 220;
  // How close to a scrollable edge the card has to be held before the board scrolls itself, and how
  // fast it then goes (per animation frame — so roughly 6px * 60fps, a column's width per second).
  const EDGE_PX = 60;
  const EDGE_STEP_PX = 6;

  const say = (text: string): void => {
    if (!note) return;
    note.textContent = text;
    note.hidden = !text;
  };

  /** Column bodies are the drop targets; the header and the padding around them are not. */
  const bodyOf = (col: HTMLElement): HTMLElement | null => col.querySelector("[data-drop]");

  /** Keep a column's count chip and its "Ingen sager her." honest after a move. */
  const refresh = (col: HTMLElement): void => {
    const cards = col.querySelectorAll(".kanban-card").length;
    const count = col.querySelector<HTMLElement>("[data-count]");
    const empty = col.querySelector<HTMLElement>("[data-empty]");
    if (count) count.textContent = String(cards);
    if (empty) empty.hidden = cards > 0;
  };

  /** Cards sit above the always-present empty-state paragraph, so a moved card lands with them. */
  const place = (card: HTMLElement, body: HTMLElement): void => {
    const empty = body.querySelector("[data-empty]");
    body.insertBefore(card, empty);
  };

  let card: HTMLElement | null = null; // the card being dragged
  let from: HTMLElement | null = null; // the column it was picked up in
  let ghost: HTMLElement | null = null; // the clone following the pointer
  let target: HTMLElement | null = null; // the column currently under the pointer
  let startX = 0;
  let startY = 0;
  let dragging = false;
  let pressTimer = 0;
  let edgeTimer = 0; // the autoscroll animation frame
  let lastX = 0; // where the pointer is, for autoscroll between pointermoves
  let lastY = 0;
  let moved = false; // a real drag happened, so the click that follows is not a click

  const highlight = (col: HTMLElement | null): void => {
    if (target === col) return;
    target?.classList.remove("is-drop-target", "is-drop-refused");
    target = col;
    if (!col) return;
    const refused = col.hasAttribute("data-manager-only") && !canManage;
    col.classList.add(refused ? "is-drop-refused" : "is-drop-target");
  };

  const begin = (event: PointerEvent): void => {
    if (!card) return;
    dragging = true;
    moved = true;
    say("");
    // Captured here and NOT on pointerdown, because a capture retargets the click that follows to
    // the capturing element: taking it on every press would mean no press ever reached the card's
    // link, and the board would be draggable but unopenable. By this point the gesture is a drag,
    // there is no click coming, and the capture is what keeps the pointer with the board when it
    // leaves the column it started in. Root rather than the card: the card is re-parented on drop,
    // and a capture on a node that moves in the DOM is released out from under the gesture.
    try {
      root.setPointerCapture(event.pointerId);
    } catch {
      // Only if the pointer is already gone. The drag still works without the capture — it just
      // ends early if the cursor leaves the board — so this is not worth abandoning the gesture for.
    }
    const box = card.getBoundingClientRect();
    ghost = card.cloneNode(true) as HTMLElement;
    ghost.classList.add("kanban-ghost");
    ghost.style.width = `${box.width}px`;
    ghost.style.left = `${box.left}px`;
    ghost.style.top = `${box.top}px`;
    ghost.dataset.grabX = String(event.clientX - box.left);
    ghost.dataset.grabY = String(event.clientY - box.top);
    document.body.append(ghost);
    card.classList.add("is-dragging");
    document.body.classList.add("kanban-dragging");
    // Scroll snapping and autoscroll cannot both be right: the board snaps to whole columns, so
    // every few pixels the edge scroll gains are snapped straight back and it never moves. Snapping
    // is what makes the board swipe one column at a time on a phone, so it is turned off for the
    // length of the drag rather than given up — on drop it comes back, and the board settles on the
    // nearest column by itself.
    root.classList.add("is-dragging-board");
    lastX = event.clientX;
    lastY = event.clientY;
    edgeTimer = window.requestAnimationFrame(autoscroll);
  };

  const follow = (event: PointerEvent): void => {
    if (!ghost) return;
    lastX = event.clientX;
    lastY = event.clientY;
    ghost.style.left = `${event.clientX - Number(ghost.dataset.grabX)}px`;
    ghost.style.top = `${event.clientY - Number(ghost.dataset.grabY)}px`;
    // The ghost is pointer-events:none, so this is whatever is genuinely underneath it.
    const under = document.elementFromPoint(event.clientX, event.clientY) as HTMLElement | null;
    highlight(under?.closest<HTMLElement>(".kanban-col") ?? null);
  };

  /** Nudge a scrollable box when the pointer is held near one of its edges. */
  const nudge = (box: Element, position: number, near: number, far: number, axis: "x" | "y"): void => {
    const before = axis === "x" ? box.scrollLeft : box.scrollTop;
    let delta = 0;
    if (position < near + EDGE_PX) delta = -EDGE_STEP_PX;
    else if (position > far - EDGE_PX) delta = EDGE_STEP_PX;
    if (!delta) return;
    if (axis === "x") box.scrollLeft = before + delta;
    else box.scrollTop = before + delta;
  };

  /** While a drag holds the page still, the board cannot be scrolled the ordinary way — and on a
   *  phone the far columns are off-screen, so without this a card could only ever be moved as far
   *  as the screen is wide. Holding the card near an edge scrolls that edge towards you: the board
   *  sideways, and the hovered column's own card list up and down. */
  const autoscroll = (): void => {
    if (!dragging) return;
    const board = root.getBoundingClientRect();
    nudge(root, lastX, board.left, board.right, "x");
    const body = target ? bodyOf(target) : null;
    if (body) {
      const column = body.getBoundingClientRect();
      nudge(body, lastY, column.top, column.bottom, "y");
    }
    edgeTimer = window.requestAnimationFrame(autoscroll);
  };

  const cleanup = (): void => {
    ghost?.remove();
    ghost = null;
    root.classList.remove("is-dragging-board");
    card?.classList.remove("is-dragging");
    document.body.classList.remove("kanban-dragging");
    highlight(null);
    card = null;
    from = null;
    dragging = false;
    window.clearTimeout(pressTimer);
    window.cancelAnimationFrame(edgeTimer);
  };

  /** Tell the server; put the card back if it says no. */
  const commit = async (moving: HTMLElement, origin: HTMLElement, column: HTMLElement): Promise<void> => {
    const status = column.dataset.status ?? "";
    const url = moving.dataset.moveUrl ?? "";
    const undo = (message: string): void => {
      const body = bodyOf(origin);
      if (body) place(moving, body);
      refresh(origin);
      refresh(column);
      say(message);
    };
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: { "X-CSRFToken": csrf, "X-Requested-With": "XMLHttpRequest" },
        body: new URLSearchParams({ status }),
      });
      if (!response.ok) {
        // A session that expired mid-board answers 302 -> the login page, which fetch follows and
        // reports as an ok HTML response; anything else lands here.
        undo("Kunne ikke flytte sagen. Prøv at genindlæse siden.");
        return;
      }
      const result = (await response.json()) as { ok: boolean; error: string; status: string };
      if (!result.ok) undo(result.error || "Sagen kunne ikke flyttes.");
      else if (result.status !== status) undo("Sagen kunne ikke flyttes.");
    } catch {
      undo("Ingen forbindelse — sagen blev ikke flyttet.");
    }
  };

  const finish = (): void => {
    const moving = card;
    const origin = from;
    const column = target;
    const wasDragging = dragging;
    cleanup();
    if (!wasDragging || !moving || !origin || !column || column === origin) return;
    if (column.hasAttribute("data-manager-only") && !canManage) {
      say("Kun Reppergruppen kan flytte en sag til denne kolonne.");
      return;
    }
    const body = bodyOf(column);
    if (!body) return;
    place(moving, body);
    refresh(origin);
    refresh(column);
    void commit(moving, origin, column);
  };

  root.addEventListener("pointerdown", (event: PointerEvent) => {
    if (event.button !== 0) return;
    const picked = (event.target as HTMLElement | null)?.closest<HTMLElement>(".kanban-card");
    if (!picked) return;
    card = picked;
    from = picked.closest<HTMLElement>(".kanban-col");
    startX = event.clientX;
    startY = event.clientY;
    moved = false;
    if (event.pointerType === "mouse") return;
    pressTimer = window.setTimeout(() => begin(event), LONG_PRESS_MS);
  });

  root.addEventListener("pointermove", (event: PointerEvent) => {
    if (!card) return;
    if (dragging) {
      // Only now is the gesture unambiguously a drag, so this is the first point at which stealing
      // the browser's own scroll/selection is the right call.
      event.preventDefault();
      follow(event);
      return;
    }
    const far = Math.abs(event.clientX - startX) > SLOP_PX || Math.abs(event.clientY - startY) > SLOP_PX;
    if (!far) return;
    if (event.pointerType === "mouse") {
      begin(event);
      follow(event);
    } else {
      // A finger that moved before the long press was the page scrolling, not a drag. Let go.
      window.clearTimeout(pressTimer);
      card = null;
      from = null;
    }
  });

  root.addEventListener("pointerup", finish);
  root.addEventListener("pointercancel", cleanup);

  // The one thing pointer events cannot do on a touchscreen: preventDefault on `pointermove` does
  // not stop a scroll, and a scroll kills the drag with `pointercancel`. Only a non-passive
  // `touchmove` can hold the page still — and only while a drag is actually running, so a finger
  // that never triggered one still scrolls the board normally.
  root.addEventListener(
    "touchmove",
    (event: Event) => {
      if (dragging) event.preventDefault();
    },
    { passive: false },
  );

  // A drag that ends on the card's own title would otherwise follow the link to the detail page.
  root.addEventListener(
    "click",
    (event: Event) => {
      if (!moved) return;
      moved = false;
      event.preventDefault();
      event.stopPropagation();
    },
    true,
  );

  // Long-pressing on a phone otherwise opens the "copy link / open in new tab" sheet over the drag.
  root.addEventListener("contextmenu", (event: Event) => {
    if (dragging) event.preventDefault();
  });

  // A card's title link is stretched over the whole card (styles.css) so that clicking anywhere on
  // the tile opens the ticket. That makes every square millimetre of it a link, and a browser's own
  // answer to "mouse down on a link, then move" is to start a NATIVE drag of the URL — which takes
  // the gesture over, swallows the pointer events this file needs, and drops a link somewhere
  // instead of moving the ticket. Refusing dragstart is what leaves the gesture to us.
  root.addEventListener("dragstart", (event: Event) => event.preventDefault());
}
