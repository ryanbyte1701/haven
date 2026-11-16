// Countdown on the "This is the standby Haven" page (app/ha.py splash_html). The redirect itself is a
// <meta http-equiv="refresh">, so it also works without JavaScript; this only updates the number.
(() => {
  const main = document.querySelector("main"), out = document.getElementById("count");
  let left = Number(main?.dataset.seconds || 0);
  if (!left || !out) return;
  const t = setInterval(() => {
    left -= 1;
    if (left <= 0) { clearInterval(t); out.textContent = "0"; location.replace(main.dataset.url); return; }
    out.textContent = String(left);
  }, 1000);
})();
