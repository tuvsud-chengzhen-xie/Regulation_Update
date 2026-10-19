/*
 * TÜV SÜD CPS — Savings Chip Celebration
 * ──────────────────────────────────────────────────────────────────
 * Listens for form submissions carrying `data-savings-km="N"` and, on
 * each, spawns a flock of flying green octagons from the submit button
 * up to the nav-bar Savings chip (#savings-counter). The chip then
 * bumps and animates its number ticking up. Form submission is
 * deferred ~750ms so the celebration is visible before the page
 * reloads — after the reload, the server-rendered chip value lands
 * and matches what the user just saw counted up.
 *
 * Usage:
 *   1. Make sure base.html has the #savings-counter element from
 *      shell.html (or your own variant of it).
 *   2. Include this file at the bottom of <body>:
 *        <script src="/ds/shell.js"></script>
 *   3. Annotate any form that represents a saving action with the
 *      delta to credit:
 *        <form ... data-savings-km="26">…</form>
 *
 * The unit ("km") is just convention — rename `data-savings-km` and
 * the `savings-value` rendering for your domain (hours, $, etc).
 *
 * No dependencies. Pure vanilla JS.
 */
(function () {
    "use strict";

    function getCounter() {
        return document.getElementById("savings-counter");
    }

    function spawnOctagons(origin, delta) {
        const counter = getCounter();
        if (!counter) return;
        const targetRect = counter.getBoundingClientRect();
        const tx = targetRect.left + targetRect.width / 2;
        const ty = targetRect.top + targetRect.height / 2;

        // Particle count scales with magnitude but stays in a tasteful range.
        const N = Math.min(14, Math.max(6, Math.round(delta / 4)));

        for (let i = 0; i < N; i++) {
            const o = document.createElement("div");
            o.className = "flying-octagon";
            const size = 18 + Math.random() * 14;
            const driftX = (Math.random() - 0.5) * 140;
            const midX = driftX * 0.4;
            const midY = -60 - Math.random() * 90;
            o.style.left = (origin.x - size / 2) + "px";
            o.style.top  = (origin.y - size / 2) + "px";
            o.style.width  = size + "px";
            o.style.height = size + "px";
            o.style.setProperty("--dx", (tx - origin.x + driftX) + "px");
            o.style.setProperty("--dy", (ty - origin.y) + "px");
            o.style.setProperty("--mx", midX + "px");
            o.style.setProperty("--my", midY + "px");
            o.style.animationDelay = (i * 55) + "ms";
            o.innerHTML =
                '<svg viewBox="0 0 100 100">' +
                  '<polygon points="30,5 70,5 95,30 95,70 70,95 30,95 5,70 5,30"' +
                          ' fill="currentColor" stroke="white" stroke-width="6"/>' +
                  '<text x="50" y="62" font-size="42" font-weight="900"' +
                        ' font-family="-apple-system,BlinkMacSystemFont,sans-serif"' +
                        ' fill="white" text-anchor="middle">+</text>' +
                '</svg>';
            document.body.appendChild(o);
            setTimeout(function (el) { return function () { el.remove(); }; }(o),
                       1900 + i * 55);
        }

        // Bump the chip + ease the number up to the new total.
        const valueEl = counter.querySelector(".savings-value");
        const current = parseFloat(counter.dataset.value || "0");
        const next    = current + delta;
        counter.dataset.value = String(next);

        if (valueEl) {
            const startTime = performance.now();
            const dur = 950;
            (function tick(now) {
                const t = Math.min((now - startTime) / dur, 1);
                const ease = 1 - Math.pow(1 - t, 3);
                const cur = Math.round(current + (next - current) * ease);
                valueEl.textContent = "~" + cur + " km";
                if (t < 1) requestAnimationFrame(tick);
            })(performance.now());
        }

        counter.classList.remove("counter-bump");
        void counter.offsetWidth;     // force reflow → restart animation
        counter.classList.add("counter-bump");
        setTimeout(function () { counter.classList.remove("counter-bump"); }, 700);
    }

    document.addEventListener("submit", function (e) {
        const form = e.target;
        const km = parseFloat(form.dataset.savingsKm || "0");
        if (!km || km <= 0) return;
        if (form.dataset.celebrating === "1") return;
        e.preventDefault();
        form.dataset.celebrating = "1";
        const trigger = e.submitter || form;
        const rect = trigger.getBoundingClientRect();
        spawnOctagons(
            { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 },
            km
        );
        // Submit for real after the animation has had time to play.
        setTimeout(function () { form.submit(); }, 750);
    });
})();
