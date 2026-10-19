/*
 * Standard Update Monitoring — UI behaviour
 * ──────────────────────────────────────────────────────────────────
 * Follows the CPS CoE data-surface rules (dashboards.md):
 *   - the page never blanks: first load shows a shape-matched skeleton,
 *     later loads keep the view and mark it pending
 *   - a filter change patches values in place; table/KPI structure is
 *     built once and never rebuilt
 *   - entrances play on mount only
 *   - every changing number is tabular, localised, and ticks from the
 *     value currently on screen
 *   - continuous controls debounce (trailing, 140ms) and every repeated
 *     request carries a sequence guard
 *
 * No AI identity here on purpose: extraction is deterministic scraping
 * and the search box is a plain table filter, so per AI-IDENTITY.md
 * there is no command bar, no swarm overlay and no glow.
 */
(function () {
    "use strict";

    const $ = (sel, root) => (root || document).querySelector(sel);
    const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

    const DEBOUNCE_MS = 140;
    const PAGE_SIZE = 50;

    const state = {
        key: localStorage.getItem("sumKey") || "",
        me: null,
        view: "standards",
        std: { q: "", status: "", body: "", offset: 0, total: 0, mounted: false },
        chg: { type: "", mounted: false },
        src: { body: "", mounted: false },
        iss: { scope: "", latest: true, mounted: false },
        alerts: [],
        selectedAlert: null,
        stats: null,
    };

    // ── helpers ──────────────────────────────────────────────────────
    const esc = (v) =>
        (v === null || v === undefined ? "" : String(v)).replace(/[&<>"']/g, (c) =>
            ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    const num = (v) => Number(v || 0).toLocaleString();
    const dash = (v) => (v ? esc(v) : '<span class="muted">—</span>');
    const when = (iso) => (iso ? esc(String(iso).slice(0, 16).replace("T", " ")) : "");

    async function api(path, options = {}) {
        const res = await fetch(path, {
            ...options,
            headers: { "X-API-Key": state.key, "Content-Type": "application/json",
                       ...(options.headers || {}) },
        });
        if (!res.ok) {
            let detail = await res.text();
            try { detail = JSON.parse(detail).detail || detail; } catch (_) {}
            throw new Error(String(detail).slice(0, 300));
        }
        if (res.status === 204) return null;
        return res.headers.get("content-type")?.includes("json") ? res.json() : res;
    }

    /* A changing figure ticks to its new value from what is on screen —
     * never from zero — and a tick already running is retargeted. */
    function tickTo(el, target, { duration = 320, format = num } = {}) {
        if (!el) return;
        const from = Number(el.dataset.value || 0);
        if (el._tick) cancelAnimationFrame(el._tick);
        if (from === target) { el.textContent = format(target); el.dataset.value = target; return; }
        const start = performance.now();
        (function step(now) {
            const p = Math.min((now - start) / duration, 1);
            const eased = 1 - Math.pow(1 - p, 3);
            el.textContent = format(Math.round(from + (target - from) * eased));
            if (p < 1) el._tick = requestAnimationFrame(step);
            else { el._tick = null; el.dataset.value = target; }
        })(start);
    }

    function debounce(fn, wait) {
        let timer;
        return function (...args) {
            clearTimeout(timer);
            timer = setTimeout(() => fn.apply(this, args), wait);
        };
    }

    /* Pending state: keep the view, drop it to 50% opacity. Opacity only —
     * nudging a whole dashboard is the "everything jumped" glitch. */
    const pending = (el, on) => el && el.classList.toggle("is-swapping", on);

    function skeletonRows(tbody, columns, rows) {
        tbody.innerHTML = Array.from({ length: rows })
            .map(() => `<tr><td colspan="${columns}" style="padding:2px 0;border:none">
                          <div class="skel skel--row"></div></td></tr>`)
            .join("");
    }

    function emptyRow(tbody, columns, message) {
        tbody.innerHTML = `<tr class="empty"><td colspan="${columns}">${esc(message)}</td></tr>`;
    }

    const bodyBadge = (b) => `<span class="srcbadge b-${esc(b)}">${esc(b)}</span>`;

    // ── topbar chip: catalogue updates caught ────────────────────────
    /* Reuses the design system's `.flying-octagon` / `.counter-bump` CSS.
     * shell.js drives that animation from form submits carrying
     * data-savings-km; this app has no form submit and counts updates, not
     * kilometres, so it triggers the same CSS contract directly. */
    function celebrate(originEl, delta) {
        const chip = $("#savings-counter");
        if (!chip || !delta) return;
        const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        const target = chip.getBoundingClientRect();
        const tx = target.left + target.width / 2;
        const ty = target.top + target.height / 2;
        const origin = (originEl || chip).getBoundingClientRect();
        const ox = origin.left + origin.width / 2;
        const oy = origin.top + origin.height / 2;

        if (!reduce) {
            const n = Math.min(14, Math.max(6, Math.round(delta / 4)));
            for (let i = 0; i < n; i++) {
                const o = document.createElement("div");
                o.className = "flying-octagon";
                const size = 18 + Math.random() * 14;
                const driftX = (Math.random() - 0.5) * 140;
                o.style.cssText =
                    `left:${ox - size / 2}px;top:${oy - size / 2}px;width:${size}px;height:${size}px`;
                o.style.setProperty("--dx", tx - ox + driftX + "px");
                o.style.setProperty("--dy", ty - oy + "px");
                o.style.setProperty("--mx", driftX * 0.4 + "px");
                o.style.setProperty("--my", -60 - Math.random() * 90 + "px");
                o.style.animationDelay = i * 55 + "ms";
                o.innerHTML =
                    '<svg viewBox="0 0 100 100"><polygon points="30,5 70,5 95,30 95,70 70,95 30,95 5,70 5,30"' +
                    ' fill="currentColor" stroke="white" stroke-width="6"/></svg>';
                document.body.appendChild(o);
                setTimeout(() => o.remove(), 1900 + i * 55);
            }
            chip.classList.remove("counter-bump");
            void chip.offsetWidth;
            chip.classList.add("counter-bump");
            setTimeout(() => chip.classList.remove("counter-bump"), 700);
        }
        const valueEl = $(".savings-value", chip);
        tickTo(valueEl, Number(chip.dataset.value || 0) + delta, { duration: 950 });
        chip.dataset.value = Number(chip.dataset.value || 0) + delta;
    }

    /* The chip total is cumulative and server-owned. Seed it once, without
     * celebrating — the celebration belongs to updates caught in this session. */
    function seedChip(total) {
        const chip = $("#savings-counter");
        if (!chip || chip.dataset.seeded) return;
        chip.dataset.seeded = "1";
        chip.dataset.value = total;
        tickTo($(".savings-value", chip), total, { duration: 600 });
    }

    // ── KPI strip: mount once, patch thereafter ──────────────────────
    const KPIS = [
        { id: "total", label: "Standards monitored", sub: "across all sources" },
        { id: "current", label: "Current", sub: "in force today" },
        { id: "withdrawn", label: "Withdrawn", sub: "superseded or removed" },
        { id: "sources", label: "Sources", sub: "committees & publishers" },
    ];

    /* First paint: the strip must already occupy its real height, or the
     * dashboard visibly jumps when the numbers land. */
    function mountKpiSkeleton() {
        $("#kpi-strip").innerHTML =
            '<div class="skel skel--tile"></div>'.repeat(KPIS.length);
        $("#body-chips").innerHTML = '<div class="skel skel--chips"></div>';
    }

    function mountKpis() {
        $("#kpi-strip").innerHTML = KPIS.map((k, i) =>
            `<div class="kpi-tile" style="--i:${i}">
                 <div class="kpi-label">${esc(k.label)}</div>
                 <div class="kpi-value" data-kpi="${k.id}" data-value="0">0</div>
                 <div class="kpi-sub" data-kpi-sub="${k.id}">${esc(k.sub)}</div>
             </div>`).join("");
    }

    function patchKpis(stats, { first = false } = {}) {
        const duration = first ? 600 : 320;
        const values = {
            total: stats.total,
            current: stats.by_status.Current || 0,
            withdrawn: stats.by_status.Withdrawn || 0,
            sources: stats.sources.active,
        };
        for (const [id, value] of Object.entries(values)) {
            tickTo($(`[data-kpi="${id}"]`), value, { duration });
        }
        $('[data-kpi-sub="sources"]').textContent =
            `${stats.sources.active} active of ${stats.sources.total}`;
        const bodies = Object.keys(stats.by_body).length;
        $('[data-kpi-sub="total"]').textContent =
            `${bodies} standards ${bodies === 1 ? "body" : "bodies"}`;

        seedChip(stats.changes_total);
        $("#kpi-strip").removeAttribute("aria-busy");
        state.stats = stats;
    }

    // ── chips ────────────────────────────────────────────────────────
    function renderScopeChips(container, options, active, onPick) {
        container.innerHTML = options.map((o) =>
            `<button class="chip${o.value === active ? " is-active" : ""}" data-value="${esc(o.value)}">
                ${esc(o.label)}${o.count === undefined ? "" :
                  `<span class="chip-count">${num(o.count)}</span>`}
             </button>`).join("");
        container.onclick = (e) => {
            const chip = e.target.closest(".chip");
            if (chip) onPick(chip.dataset.value);
        };
    }

    function renderActiveChips(container, filters, onClear, onClearAll) {
        const chips = filters.filter((f) => f.value);
        container.innerHTML =
            chips.map((f) =>
                `<span class="activechip">${esc(f.label)}: <b>${esc(f.value)}</b>
                     <button data-key="${esc(f.key)}" aria-label="Clear ${esc(f.label)} filter">✕</button>
                 </span>`).join("") +
            (chips.length > 1 ? `<button class="activechip activechip--clear" data-clear-all>Clear all</button>` : "");
        container.onclick = (e) => {
            if (e.target.closest("[data-clear-all]")) return onClearAll();
            const btn = e.target.closest("button[data-key]");
            if (btn) onClear(btn.dataset.key);
        };
    }

    // ── standards view ───────────────────────────────────────────────
    let stdSeq = 0;
    async function loadStandards({ first = false } = {}) {
        const seq = ++stdSeq;
        const tbody = $("#t-standards tbody");
        const result = $("#std-result");

        if (first) { mountKpiSkeleton(); skeletonRows(tbody, 7, 12); }
        else pending(result, true);

        const params = new URLSearchParams({ limit: PAGE_SIZE, offset: state.std.offset });
        if (state.std.q) params.set("q", state.std.q);
        if (state.std.status) params.set("status", state.std.status);
        if (state.std.body) params.set("body", state.std.body);

        try {
            const [page, stats] = await Promise.all([
                api("/api/standards?" + params),
                api("/api/standards/statistics"),
            ]);
            if (seq !== stdSeq) return;               // superseded mid-flight

            if (!state.std.mounted) { mountKpis(); state.std.mounted = true; }
            patchKpis(stats, { first });

            renderScopeChips(
                $("#body-chips"),
                [{ label: "All bodies", value: "", count: stats.total }].concat(
                    Object.entries(stats.by_body)
                        .sort((a, b) => b[1] - a[1])
                        .map(([b, n]) => ({ label: b, value: b, count: n }))),
                state.std.body,
                (value) => { state.std.body = value; state.std.offset = 0; loadStandards(); });

            renderActiveChips(
                $("#std-activechips"),
                [{ key: "q", label: "Search", value: state.std.q },
                 { key: "status", label: "Status", value: state.std.status },
                 { key: "body", label: "Body", value: state.std.body }],
                (key) => {
                    state.std[key] = "";
                    if (key === "q") $("#q").value = "";
                    if (key === "status") $("#f-status").value = "";
                    state.std.offset = 0; loadStandards();
                },
                () => {
                    Object.assign(state.std, { q: "", status: "", body: "", offset: 0 });
                    $("#q").value = ""; $("#f-status").value = ""; loadStandards();
                });

            state.std.total = page.total;
            tbody.innerHTML = page.items.length
                ? page.items.map((r) =>
                    `<tr><td class="id" title="${esc(r.standard_id)}">${esc(r.standard_id)}</td>
                         <td><span class="srcbadge">${esc(r.tc_code)}</span></td>
                         <td title="${esc(r.title_en)}">${dash(r.title_en)}</td>
                         <td>${dash(r.title_zh)}</td>
                         <td class="num">${dash(r.publish_date)}</td>
                         <td class="num">${dash(r.implement_date)}</td>
                         <td><span class="badge is-${r.status.toLowerCase()}">${esc(r.status)}</span></td>
                     </tr>`).join("")
                : "";
            if (!page.items.length) emptyRow(tbody, 7, "No standards match these filters.");

            const filtered = state.std.q || state.std.status || state.std.body;
            $("#std-count").innerHTML = filtered
                ? `<b>${num(page.total)}</b> of ${num(stats.total)} matched`
                : `<b>${num(page.total)}</b> records`;
            $("#std-page").textContent = page.total
                ? `${num(state.std.offset + 1)}–${num(Math.min(state.std.offset + PAGE_SIZE, page.total))}`
                : "";
            $("#prev").disabled = state.std.offset === 0;
            $("#next").disabled = state.std.offset + PAGE_SIZE >= page.total;
        } catch (err) {
            if (seq === stdSeq) { emptyRow(tbody, 7, "Could not load standards."); toastError(err); }
        } finally {
            if (seq === stdSeq) pending(result, false);
        }
    }

    // ── change log ───────────────────────────────────────────────────
    const CHANGE_TYPES = ["added", "withdrawn", "reinstated", "modified"];
    let chgSeq = 0;
    async function loadChanges({ first = false } = {}) {
        const seq = ++chgSeq;
        const tbody = $("#t-changes tbody");
        if (first) skeletonRows(tbody, 7, 10); else pending($("#chg-result"), true);

        renderScopeChips(
            $("#change-chips"),
            [{ label: "All changes", value: "" }].concat(
                CHANGE_TYPES.map((t) => ({ label: t[0].toUpperCase() + t.slice(1), value: t }))),
            state.chg.type,
            (value) => { state.chg.type = value; loadChanges(); });
        renderActiveChips(
            $("#chg-activechips"),
            [{ key: "type", label: "Change", value: state.chg.type }],
            () => { state.chg.type = ""; loadChanges(); },
            () => { state.chg.type = ""; loadChanges(); });

        const params = new URLSearchParams({ limit: 200 });
        if (state.chg.type) params.set("change_type", state.chg.type);
        try {
            const rows = await api("/api/runs/changes?" + params);
            if (seq !== chgSeq) return;
            tbody.innerHTML = rows.map((r) =>
                `<tr><td class="num">#${r.run_id}</td>
                     <td><span class="badge is-${esc(r.change_type)}">${esc(r.change_type)}</span></td>
                     <td class="id" title="${esc(r.standard_id)}">${esc(r.standard_id)}</td>
                     <td><span class="srcbadge">${esc(r.tc_code)}</span></td>
                     <td title="${esc(r.title_en)}">${dash(r.title_en)}</td>
                     <td class="muted">${esc(r.old_status || "—")} → ${esc(r.new_status)}</td>
                     <td class="muted">${when(r.created_at)}</td></tr>`).join("");
            if (!rows.length) emptyRow(tbody, 7, "No changes recorded yet. Run monitoring to compare the sources against the database.");
        } catch (err) {
            if (seq === chgSeq) { emptyRow(tbody, 7, "Could not load the change log."); toastError(err); }
        } finally {
            if (seq === chgSeq) pending($("#chg-result"), false);
        }
    }

    // ── runs ─────────────────────────────────────────────────────────
    async function loadRuns({ first = false } = {}) {
        const tbody = $("#t-runs tbody");
        if (first) skeletonRows(tbody, 9, 6); else pending($("#runs-result"), true);
        try {
            const [runs, schedule] = await Promise.all([
                api("/api/runs?limit=20"), api("/api/runs/schedule"),
            ]);
            tbody.innerHTML = runs.map((r) =>
                `<tr><td class="num">#${r.id}</td><td class="muted">${when(r.started_at)}</td>
                     <td>${esc(r.trigger)}</td>
                     <td><span class="badge is-${esc(r.status)}">${esc(r.status)}</span></td>
                     <td class="num">${num(r.extracted_count)}</td>
                     <td class="num">${num(r.added_count)}</td>
                     <td class="num">${num(r.withdrawn_count)}</td>
                     <td class="num">${r.failed_record_count
                         ? `<a href="#/issues">${num(r.failed_record_count)}</a>`
                         : "0"}</td>
                     <td class="num">${num(r.alerts_sent)}</td></tr>`).join("");
            if (!runs.length) emptyRow(tbody, 9, "No runs yet.");

            const last = runs[0];
            $("#rail-lastrun").innerHTML = last
                ? `<div class="rail-stat"><span>Run</span><span>#${last.id}</span></div>
                   <div class="rail-stat"><span>Status</span><span>${esc(last.status)}</span></div>
                   <div class="rail-stat"><span>Extracted</span><span>${num(last.extracted_count)}</span></div>
                   <div class="rail-stat"><span>Added</span><span>${num(last.added_count)}</span></div>
                   <div class="rail-stat"><span>Withdrawn</span><span>${num(last.withdrawn_count)}</span></div>
                   <div class="rail-stat"><span>Alerts sent</span><span>${num(last.alerts_sent)}</span></div>`
                : `<p class="muted">No runs yet.</p>`;

            const failedSources = last
                ? last.results.filter((r) => r.status === "failed").length : 0;
            $("#rail-issues").innerHTML = last
                ? `<div class="rail-stat"><span>Entries lost</span>
                       <span>${num(last.failed_record_count)}</span></div>
                   <div class="rail-stat"><span>Sources missed</span>
                       <span>${num(failedSources)}</span></div>
                   ${last.failed_record_count || failedSources
                       ? `<a class="rail-link" href="#/issues">See what was missed →</a>`
                       : `<p class="muted">Everything on every source page was captured.</p>`}`
                : `<p class="muted">No runs yet.</p>`;

            $("#rail-schedule").innerHTML = schedule.enabled
                ? `<p>Day ${esc(schedule.cron.day)} of each month at
                      ${esc(schedule.cron.hour)}:${String(schedule.cron.minute).padStart(2, "0")} UTC.</p>
                   <p class="muted">Next: ${when(schedule.next_run_time) || "n/a"}</p>`
                : `<p class="muted">Scheduler disabled. Set <code>SCHEDULER_ENABLED=true</code>
                      to monitor on a schedule.</p>`;
        } catch (err) {
            emptyRow(tbody, 9, "Could not load runs."); toastError(err);
        } finally { pending($("#runs-result"), false); }
    }

    async function runMonitoring(originEl) {
        const buttons = $$("#btn-run, #btn-run-live");
        buttons.forEach((b) => (b.disabled = true));
        showToast(
            "Fetching every source live — ISO, BIFMA, ALI and WSTDA go through a browser, " +
            "so this takes a few minutes.",
            { type: "info", duration: 6000 });
        try {
            const run = await api("/api/runs", { method: "POST", body: JSON.stringify({}) });
            const caught = run.added_count + run.withdrawn_count + run.reinstated_count + run.modified_count;
            const failedSources = run.results.filter((r) => r.status === "failed").length;

            if (caught) celebrate(originEl, caught);
            showToast(
                `Run #${run.id} ${run.status} — +${run.added_count} added, ` +
                `${run.withdrawn_count} withdrawn, ${run.alerts_sent} alert(s)`,
                { type: caught ? "success" : "info", duration: 5000 });

            // Never let a gap pass silently: say what was missed and where to look.
            if (run.failed_record_count) {
                showToast(
                    `${run.failed_record_count} catalogue entr` +
                    `${run.failed_record_count === 1 ? "y was" : "ies were"} seen but could not ` +
                    `be captured — open Issues to see which.`,
                    { type: "warn", duration: 9000 });
            }
            if (failedSources) {
                showToast(
                    `${failedSources} source(s) could not be fetched; their standards were ` +
                    `left untouched rather than withdrawn.`,
                    { type: "error", duration: 9000 });
            }
            await Promise.all([
                loadStandards(), loadChanges(), loadRuns(), loadSources(), loadIssues(),
            ]);
        } catch (err) {
            toastError(err);
        } finally {
            buttons.forEach((b) => (b.disabled = false));
        }
    }


    // ── extraction issues ────────────────────────────────────────────
    /* The point of this surface: a catalogue entry we failed to capture must
     * never be mistaken for a standard that left the catalogue. */
    const ISSUE_KPIS = [
        { id: "total", label: "Issues", sub: "" },
        { id: "record", label: "Entries lost", sub: "not in the database" },
        { id: "field", label: "Incomplete", sub: "captured with a gap" },
        { id: "source", label: "Sources missed", sub: "left untouched" },
    ];
    const SCOPE_LABEL = { record: "Lost", field: "Incomplete", source: "Source" };
    const SCOPE_BADGE = { record: "withdrawn", field: "modified", source: "failed" };

    function mountIssueKpis() {
        $("#issue-kpis").innerHTML = ISSUE_KPIS.map((k, i) =>
            `<div class="kpi-tile" style="--i:${i}">
                 <div class="kpi-label">${esc(k.label)}</div>
                 <div class="kpi-value" data-ikpi="${k.id}" data-value="0">0</div>
                 <div class="kpi-sub">${esc(k.sub)}</div>
             </div>`).join("");
    }

    let issSeq = 0;
    async function loadIssues({ first = false } = {}) {
        const seq = ++issSeq;
        const tbody = $("#t-issues tbody");
        if (first) {
            $("#issue-kpis").innerHTML = '<div class="skel skel--tile"></div>'.repeat(4);
            skeletonRows(tbody, 7, 8);
        } else {
            pending($("#iss-result"), true);
        }

        const params = new URLSearchParams({ limit: 500 });
        if (state.iss.latest) params.set("latest", "true");
        if (state.iss.scope) params.set("scope", state.iss.scope);

        try {
            const [rows, summary] = await Promise.all([
                api("/api/runs/issues?" + params),
                api("/api/runs/issues/summary" + (state.iss.latest ? "?latest=true" : "")),
            ]);
            if (seq !== issSeq) return;

            if (!state.iss.mounted) { mountIssueKpis(); state.iss.mounted = true; }
            const values = {
                total: summary.total,
                record: summary.lost_records,
                field: summary.incomplete_records,
                source: summary.failed_sources,
            };
            for (const [id, value] of Object.entries(values)) {
                tickTo($(`[data-ikpi="${id}"]`), value, { duration: first ? 600 : 320 });
            }
            $('[data-ikpi="total"]').closest(".kpi-tile").querySelector(".kpi-sub").textContent =
                state.iss.latest
                    ? `on run #${summary.run_id ?? "—"}`
                    : "across all runs";

            renderScopeChips(
                $("#issue-chips"),
                [{ label: "All issues", value: "", count: summary.total }].concat(
                    ["record", "field", "source"]
                        .filter((k) => summary.by_scope[k])
                        .map((k) => ({ label: SCOPE_LABEL[k], value: k, count: summary.by_scope[k] }))),
                state.iss.scope,
                (value) => { state.iss.scope = value; loadIssues(); });

            renderActiveChips(
                $("#iss-activechips"),
                [{ key: "scope", label: "Scope", value: SCOPE_LABEL[state.iss.scope] || "" },
                 { key: "latest", label: "Run", value: state.iss.latest ? "latest only" : "" }],
                (key) => {
                    if (key === "scope") state.iss.scope = "";
                    if (key === "latest") state.iss.latest = false;
                    loadIssues();
                },
                () => { state.iss.scope = ""; state.iss.latest = false; loadIssues(); });

            tbody.innerHTML = rows.map((r) =>
                `<tr><td class="num">#${r.run_id}</td>
                     <td><span class="badge is-${SCOPE_BADGE[r.scope] || "modified"}">${
                         esc(SCOPE_LABEL[r.scope] || r.scope)}</span></td>
                     <td>${bodyBadge(r.body || "")} ${esc(r.tc_code || "")}</td>
                     <td class="id" title="${esc(r.standard_id)}">${dash(r.standard_id)}</td>
                     <td title="${esc(r.detail)}">${esc(r.reason.replace(/_/g, " "))}</td>
                     <td title="${esc(r.raw || r.detail)}">${dash(r.raw)}</td>
                     <td class="muted">${dash(r.location)}</td></tr>`).join("");
            if (!rows.length) {
                emptyRow(tbody, 7,
                    state.iss.latest
                        ? "Nothing was missed on the last run — every entry on every source page was captured."
                        : "No extraction issues recorded.");
            }
            $("#iss-count").innerHTML = `<b>${num(rows.length)}</b> shown`;
            $("#btn-issues-all").textContent =
                state.iss.latest ? "Show all runs" : "Latest run only";
            $("#issue-kpis").removeAttribute("aria-busy");
        } catch (err) {
            if (seq === issSeq) { emptyRow(tbody, 7, "Could not load extraction issues."); toastError(err); }
        } finally {
            if (seq === issSeq) pending($("#iss-result"), false);
        }
    }

    // ── sources ──────────────────────────────────────────────────────
    async function loadSources({ first = false } = {}) {
        const tbody = $("#t-sources tbody");
        if (first) skeletonRows(tbody, 7, 8); else pending($("#src-result"), true);
        try {
            const rows = await api("/api/sources");
            const bodies = [...new Set(rows.map((r) => r.body))];
            renderScopeChips(
                $("#source-chips"),
                [{ label: "All bodies", value: "", count: rows.length }].concat(
                    bodies.map((b) => ({ label: b, value: b,
                                         count: rows.filter((r) => r.body === b).length }))),
                state.src.body,
                (value) => { state.src.body = value; loadSources(); });
            renderActiveChips(
                $("#src-activechips"),
                [{ key: "body", label: "Body", value: state.src.body }],
                () => { state.src.body = ""; loadSources(); },
                () => { state.src.body = ""; loadSources(); });

            const shown = state.src.body ? rows.filter((r) => r.body === state.src.body) : rows;
            tbody.innerHTML = shown.map((r) =>
                `<tr><td>${bodyBadge(r.body)}</td>
                     <td class="id">${esc(r.tc_code)}</td>
                     <td title="${esc(r.title)}">${dash(r.title)} —
                         <a href="${esc(r.url)}" target="_blank" rel="noopener">source</a></td>
                     <td class="num">${num(r.standard_count)}</td>
                     <td class="num">${num(r.current_count)}</td>
                     <td><span class="srcbadge">${esc(r.fetch_strategy)}</span></td>
                     <td class="muted">${when(r.last_run_at) || "—"}</td></tr>`).join("");
            if (!shown.length) emptyRow(tbody, 7, "No sources.");
        } catch (err) {
            emptyRow(tbody, 7, "Could not load sources."); toastError(err);
        } finally { pending($("#src-result"), false); }
    }

    // ── subscriptions & alerts ───────────────────────────────────────
    async function loadSubscriptions({ first = false } = {}) {
        const tbody = $("#t-subs tbody");
        if (first) skeletonRows(tbody, 4, 4); else pending($("#subs-result"), true);
        try {
            const [subs, alerts] = await Promise.all([
                api("/api/subscriptions"), api("/api/subscriptions/alerts?limit=25"),
            ]);
            tbody.innerHTML = subs.map((s) =>
                `<tr><td>${esc(s.type)}</td>
                     <td><span class="srcbadge">${esc(s.value)}</span></td>
                     <td>${s.is_active ? "Yes" : "No"}</td>
                     <td><button class="btn btn-sm" data-unsub="${s.id}">Remove</button></td></tr>`).join("");
            if (!subs.length) emptyRow(tbody, 4, "No subscriptions yet. Add one in the panel on the right.");

            state.alerts = alerts;
            $("#alert-list").innerHTML = alerts.length
                ? alerts.map((a) =>
                    `<button class="mrow" data-alert="${a.id}">
                        <div class="mrow-title">Run #${a.run_id} — ${num(a.change_count)} update(s)</div>
                        <div class="mrow-meta">${when(a.sent_at)} · ${esc(a.status)} · ${esc(a.backend)}</div>
                     </button>`).join("")
                : `<p class="muted" style="padding:var(--space-3)">No alerts yet.</p>`;
        } catch (err) {
            emptyRow(tbody, 4, "Could not load subscriptions."); toastError(err);
        } finally { pending($("#subs-result"), false); }
    }

    async function showAlert(id) {
        const pane = $("#alert-pane");
        $$("#alert-list .mrow").forEach((r) =>
            r.classList.toggle("is-selected", r.dataset.alert === String(id)));
        pane.classList.add("is-swapping-pane");
        try {
            const alert = await api(`/api/subscriptions/alerts/${id}`);
            pane.innerHTML =
                `<div class="eyebrow">${when(alert.sent_at)} · to ${esc(alert.email)}</div>
                 <h2 style="margin:6px 0 var(--space-3);font-size:var(--font-size-md)">${esc(alert.subject)}</h2>
                 <pre class="pre">${esc(alert.body)}</pre>`;
        } catch (err) {
            pane.innerHTML = `<p class="muted">Could not load this alert.</p>`;
            toastError(err);
        } finally {
            requestAnimationFrame(() => pane.classList.remove("is-swapping-pane"));
        }
    }

    // ── export ───────────────────────────────────────────────────────
    async function exportExcel() {
        showToast("Building the workbook…", { type: "info", duration: 2000 });
        try {
            const res = await fetch("/api/export/standards.xlsx", { headers: { "X-API-Key": state.key } });
            if (!res.ok) throw new Error((await res.text()).slice(0, 200));
            const blob = await res.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = "standards_database.xlsx";
            a.click();
            URL.revokeObjectURL(url);
            showToast("Excel export downloaded", { type: "success" });
        } catch (err) { toastError(err); }
    }

    const toastError = (err) =>
        showToast(esc(err.message || "Something went wrong"), { type: "error", duration: 5500 });

    // ── routing ──────────────────────────────────────────────────────
    const VIEWS = {
        standards: () => loadStandards({ first: !state.std.mounted }),
        changes: () => loadChanges({ first: !state.chg.mounted && (state.chg.mounted = true) }),
        runs: () => loadRuns({ first: !VIEWS._runs && (VIEWS._runs = true) }),
        issues: () => loadIssues({ first: !state.iss.mounted }),
        sources: () => loadSources({ first: !VIEWS._sources && (VIEWS._sources = true) }),
        subscriptions: () => loadSubscriptions({ first: !VIEWS._subs && (VIEWS._subs = true) }),
        admin: () => {},
    };

    function route() {
        const wanted = (location.hash.replace("#/", "") || "standards").split("?")[0];
        const view = state.key && state.me ? (VIEWS[wanted] ? wanted : "standards") : "connect";
        state.view = view;

        $$(".view").forEach((el) => el.classList.toggle("is-active", el.id === `view-${view}`));
        $$("#topnav a, .topnav-setup").forEach((a) =>
            a.classList.toggle("is-active", a.dataset.view === view));
        document.title =
            `${view[0].toUpperCase() + view.slice(1)} — Standard Update Monitoring`;
        if (VIEWS[view]) VIEWS[view]();
    }

    async function connect(key) {
        state.key = key;
        try {
            state.me = await api("/api/users/me");
            localStorage.setItem("sumKey", key);
            $("#footer-text").textContent =
                `Signed in as ${state.me.email} (${state.me.role}) — by CPS Center of Excellence`;
            $("#admin-sub").textContent = `${state.me.email} · role: ${state.me.role}`;
            const admin = state.me.role === "admin";
            $$("#btn-run, #btn-export, #btn-export2, #btn-run-live, #btn-quality, #btn-sessions")
                .forEach((b) => { b.hidden = !admin; });
            if (!location.hash || location.hash === "#/connect") location.hash = "#/standards";
            route();
            api("/api/standards/statistics")
                .then((stats) => seedChip(stats.changes_total))
                .catch(() => {});
            showToast(`Signed in as ${esc(state.me.email)}`, { type: "success" });
        } catch (err) {
            state.me = null;
            state.key = "";
            localStorage.removeItem("sumKey");
            route();
            toastError(err);
        }
    }

    // ── wiring ───────────────────────────────────────────────────────
    $("#connect").onclick = () => connect($("#key").value.trim());
    $("#key").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#connect").click(); });

    const searchStandards = debounce(() => {
        state.std.q = $("#q").value.trim();
        state.std.offset = 0;
        loadStandards();
    }, DEBOUNCE_MS);
    $("#q").addEventListener("input", searchStandards);
    $("#f-status").addEventListener("change", () => {
        state.std.status = $("#f-status").value;
        state.std.offset = 0;
        loadStandards();
    });
    $("#prev").onclick = () => {
        state.std.offset = Math.max(0, state.std.offset - PAGE_SIZE);
        loadStandards();
    };
    $("#next").onclick = () => {
        if (state.std.offset + PAGE_SIZE < state.std.total) {
            state.std.offset += PAGE_SIZE;
            loadStandards();
        }
    };

    $("#btn-run").onclick = (e) => runMonitoring(e.currentTarget);
    $("#btn-run-live").onclick = (e) => runMonitoring(e.currentTarget);
    $("#btn-issues-all").onclick = () => {
        state.iss.latest = !state.iss.latest;
        loadIssues();
    };
    $("#btn-export").onclick = exportExcel;
    $("#btn-export2").onclick = exportExcel;

    $("#t-subs").addEventListener("click", async (e) => {
        const btn = e.target.closest("[data-unsub]");
        if (!btn) return;
        try {
            await api(`/api/subscriptions/${btn.dataset.unsub}`, { method: "DELETE" });
            showToast("Subscription removed", { type: "success" });
            loadSubscriptions();
        } catch (err) { toastError(err); }
    });
    $("#alert-list").addEventListener("click", (e) => {
        const row = e.target.closest("[data-alert]");
        if (row) showAlert(row.dataset.alert);
    });
    $("#s-add").onclick = async () => {
        const value = $("#s-value").value.trim();
        if (!value) return showToast("Enter something to subscribe to", { type: "warn" });
        try {
            await api("/api/subscriptions", {
                method: "POST",
                body: JSON.stringify({ type: $("#s-type").value, value }),
            });
            $("#s-value").value = "";
            showToast("Subscribed", { type: "success" });
            loadSubscriptions();
        } catch (err) { toastError(err); }
    };

    const showJson = (path) => async () => {
        $("#admin-out").textContent = "Loading…";
        try { $("#admin-out").textContent = JSON.stringify(await api(path), null, 2); }
        catch (err) { $("#admin-out").textContent = err.message; toastError(err); }
    };
    $("#btn-quality").onclick = showJson("/ai_training_quality");
    $("#btn-sessions").onclick = showJson("/ai_training_sessions");
    $("#btn-signout").onclick = () => {
        localStorage.removeItem("sumKey");
        state.key = ""; state.me = null;
        location.hash = "#/connect";
        route();
        showToast("Signed out", { type: "info" });
    };

    window.addEventListener("hashchange", route);

    // ── keyboard shortcuts + command palette ─────────────────────────
    const go = (view) => () => { location.hash = "#/" + view; };
    NudgeSystem.init({
        shortcuts: [
            { key: "k", mod: "cmd", label: "Command palette", keys: ["⌘", "K"],
              section: "General", action: () => toggleCommandPalette() },
            { key: "?", label: "Keyboard shortcuts", keys: ["?"],
              section: "General", action: () => toggleShortcutsOverlay() },
            { key: "/", label: "Focus search", keys: ["/"], section: "Standards",
              action: () => { location.hash = "#/standards"; setTimeout(() => $("#q").focus(), 60); } },
            { key: "r", label: "Run monitoring", keys: ["R"],
              section: "Monitoring", action: () => runMonitoring($("#btn-run")) },
            { key: "6", label: "Extraction issues", keys: ["6"], section: "Navigate",
              action: () => { location.hash = "#/issues"; } },
            { key: "e", label: "Export Excel", keys: ["E"], section: "Monitoring", action: exportExcel },
            { key: "1", label: "Standards", keys: ["1"], section: "Navigate", action: go("standards") },
            { key: "2", label: "Change log", keys: ["2"], section: "Navigate", action: go("changes") },
            { key: "3", label: "Runs", keys: ["3"], section: "Navigate", action: go("runs") },
            { key: "4", label: "Sources", keys: ["4"], section: "Navigate", action: go("sources") },
            { key: "5", label: "Subscriptions", keys: ["5"], section: "Navigate", action: go("subscriptions") },
        ],
        commands: [
            { label: "Run monitoring now", keys: ["R"], tags: "run extract monitor live fetch",
              action: () => runMonitoring($("#btn-run")) },
            { label: "Show what could not be extracted", keys: ["6"],
              tags: "issues failed missing lost gaps errors",
              action: () => { location.hash = "#/issues"; } },
            { label: "Export database to Excel", keys: ["E"], tags: "export excel xlsx download",
              action: exportExcel },
            { label: "Go to Standards", keys: ["1"], tags: "standards database", action: go("standards") },
            { label: "Go to Change log", keys: ["2"], tags: "changes withdrawn added", action: go("changes") },
            { label: "Go to Runs", keys: ["3"], tags: "runs monitoring schedule", action: go("runs") },
            { label: "Go to Sources", keys: ["4"], tags: "sources committees tc sc", action: go("sources") },
            { label: "Go to Subscriptions", keys: ["5"], tags: "subscriptions alerts email", action: go("subscriptions") },
            { label: "Show only Current standards", keys: [], tags: "filter status current",
              action: () => { $("#f-status").value = "Current"; $("#f-status").dispatchEvent(new Event("change")); } },
            { label: "Show only Withdrawn standards", keys: [], tags: "filter status withdrawn",
              action: () => { $("#f-status").value = "Withdrawn"; $("#f-status").dispatchEvent(new Event("change")); } },
            { label: "Training-data quality report", keys: [], tags: "ai training quality jsonl",
              action: () => { location.hash = "#/admin"; $("#btn-quality").click(); } },
            { label: "Sign out", keys: [], tags: "logout signout", action: () => $("#btn-signout").click() },
        ],
    });

    // ── boot ─────────────────────────────────────────────────────────
    $("#key").value = state.key;
    if (state.key) connect(state.key); else route();
})();
