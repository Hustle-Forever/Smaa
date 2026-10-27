// Whether an element is on screen: in the document, under no `hidden`
// ancestor and laid out. The app keeps the Producer page mounted under
// <div hidden> while another tab is open (App.tsx), so a window or document
// key listener of anything on it (the creator's ⌘/Ctrl+Enter, the Rebrand
// studio's Generate and undo, a picker's Esc) checks this first: its keys
// never act, or swallow another page's keys, from another tab.

export function isShown(el: Element | null | undefined): boolean {
  return !!el && el.isConnected && !el.closest("[hidden]") && el.getClientRects().length > 0;
}
