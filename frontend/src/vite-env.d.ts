/// <reference types="vite/client" />

// Alpine.js 3 ships no type declarations and there is no maintained @types package for v3.
// We only use it for `Alpine.start()`, so an ambient `any` module is sufficient.
declare module "alpinejs";

// idiomorph ships no type declarations (its package.json has no `types` field), so this declares
// exactly the sliver frontend/src/feed.ts touches. Importing the module registers htmx's `morph`
// extension as a side effect; `Idiomorph.defaults` is the only supported way to configure its
// behaviour globally. Kept deliberately narrow rather than `declare module "idiomorph/htmx";`,
// which would type the whole thing as `any` and hide a typo in the two callbacks we rely on.
declare module "idiomorph/htmx" {
  export const Idiomorph: {
    defaults: {
      ignoreActiveValue: boolean;
      restoreFocus: boolean;
      callbacks: {
        beforeAttributeUpdated: (
          attributeName: string,
          node: Element,
          mutationType: "update" | "remove",
        ) => boolean;
        beforeNodeMorphed: (oldNode: Node, newNode: Node) => boolean;
      };
    };
  };
}
