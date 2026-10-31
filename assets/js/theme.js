// Loaded blocking in <head> so the saved theme is applied before the page paints (no light flash in dark mode).
// The choice is saved in localStorage("theme") by the toggle in app.js; with no saved choice, follow the OS setting.
(() => {
  const root = document.documentElement;
  root.classList.add("hx-js");  // CSS hides animate-in elements only when JS is there to reveal them
  let saved = null;
  try { saved = localStorage.getItem("theme"); } catch {}
  root.dataset.mdbTheme = saved === "dark" || saved === "light" ? saved : (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  // The navbar drops in on the first page of a tab only; after that it stays put between pages, like an SPA's persistent nav
  try { if (!sessionStorage.getItem("hx-nav")) { root.classList.add("hx-nav-intro"); sessionStorage.setItem("hx-nav", "1"); } } catch { root.classList.add("hx-nav-intro"); }
})();
