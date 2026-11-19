// Marks a board search's words in the cards: <mark> around matching text inside [data-highlight-in]
// elements. Text nodes only, so the rendered Markdown's links, mentions and tables are untouched.
// The words come from the server (opslagstavle/search.py highlight_terms), longest first.

const terms: string[] = (() => {
  const data = document.getElementById("board-highlight");
  try {
    return data ? (JSON.parse(data.textContent ?? "[]") as string[]) : [];
  } catch {
    return [];
  }
})();

const escape = (term: string): string => term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const pattern = terms.length ? new RegExp(terms.map(escape).join("|"), "giu") : null;

function markText(node: Text): void {
  if (!pattern) return;
  const text = node.data;
  pattern.lastIndex = 0;
  if (!pattern.test(text)) return;
  pattern.lastIndex = 0;
  const parts = document.createDocumentFragment();
  let at = 0;
  for (const match of text.matchAll(pattern)) {
    const index = match.index ?? 0;
    if (index > at) parts.append(text.slice(at, index));
    const mark = document.createElement("mark");
    mark.className = "search-hit";
    mark.textContent = match[0];
    parts.append(mark);
    at = index + match[0].length;
  }
  if (at < text.length) parts.append(text.slice(at));
  node.replaceWith(parts);
}

/** Marks every match under `root`. Safe to call again on a re-rendered card. */
export function highlight(root: ParentNode = document): void {
  if (!pattern) return;
  for (const scope of root.querySelectorAll<HTMLElement>("[data-highlight-in]")) {
    const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT, {
      acceptNode: (node) =>
        node.parentElement?.closest("mark, script, style, code, pre")
          ? NodeFilter.FILTER_REJECT
          : NodeFilter.FILTER_ACCEPT,
    });
    const nodes: Text[] = [];
    while (walker.nextNode()) nodes.push(walker.currentNode as Text);
    nodes.forEach(markText);
  }
}

highlight();
