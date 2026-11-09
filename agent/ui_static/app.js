/* Step 55 — Jarvis mission-control UI, vanilla JavaScript only.
 *
 * No frameworks, no CDN, no network calls. The page embeds the
 * data it needs in <script type="application/json"> blocks; this
 * file only draws charts from that data, sorts tables, and fills
 * the chat box when a command chip is clicked.
 */
(function () {
  "use strict";

  /* -- DCF range bar chart -------------------------------------------
   * data: [{ticker, price, conservative, base, optimistic, band}]
   * Draws one horizontal group per ticker: three bars for the DCF
   * range (conservative / base / optimistic) and a vertical marker
   * for the live price. Colours match the CSS badge palette.
   */
  function drawDcfChart(canvas) {
    var dataEl = document.getElementById(canvas.dataset.jsonId);
    if (!dataEl) { return; }
    var rows;
    try {
      rows = JSON.parse(dataEl.textContent);
    } catch (e) {
      return;
    }
    if (!rows || !rows.length) { return; }
    var ctx = canvas.getContext("2d");
    if (!ctx) { return; }

    var dpr = window.devicePixelRatio || 1;
    var width = canvas.clientWidth || 900;
    var rowH = 34;
    var barH = 7;
    var height = rows.length * rowH + 34;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.height = height + "px";
    ctx.scale(dpr, dpr);

    var maxVal = 0;
    rows.forEach(function (r) {
      [r.price, r.conservative, r.base, r.optimistic].forEach(function (v) {
        if (typeof v === "number" && v > maxVal) { maxVal = v; }
      });
    });
    if (maxVal <= 0) { return; }
    var left = 64;
    var right = 14;
    var plotW = width - left - right;
    function x(v) { return left + (v / maxVal) * plotW; }

    ctx.font = "12px 'Segoe UI', Arial, sans-serif";
    ctx.textBaseline = "middle";

    rows.forEach(function (r, i) {
      var y = 18 + i * rowH;
      ctx.fillStyle = "#d7e6f5";
      ctx.textAlign = "right";
      ctx.fillText(r.ticker, left - 8, y + barH * 2);
      // DCF bars: conservative (amber), base (cyan), optimistic (violet).
      var bars = [
        [r.conservative, "#f59e0b"],
        [r.base, "#22d3ee"],
        [r.optimistic, "#a78bfa"]
      ];
      bars.forEach(function (pair, j) {
        var val = pair[0];
        if (typeof val !== "number") { return; }
        ctx.fillStyle = pair[1];
        var bw = Math.max(2, x(val) - left);
        ctx.fillRect(left, y + j * barH, bw, barH - 1);
      });
      // Price marker: a bright vertical line across the group.
      if (typeof r.price === "number") {
        ctx.strokeStyle = "#f87171";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(x(r.price), y - 3);
        ctx.lineTo(x(r.price), y + barH * 3 + 1);
        ctx.stroke();
        ctx.fillStyle = "#f87171";
        ctx.textAlign = "left";
        ctx.fillText("$" + r.price.toFixed(2), x(r.price) + 4, y + barH);
      }
    });

    // Legend.
    var legendY = height - 14;
    ctx.textAlign = "left";
    var legend = [
      ["#f59e0b", "conservative"],
      ["#22d3ee", "base"],
      ["#a78bfa", "optimistic"],
      ["#f87171", "price"]
    ];
    var lx = left;
    legend.forEach(function (item) {
      ctx.fillStyle = item[0];
      ctx.fillRect(lx, legendY - 5, 12, 10);
      ctx.fillStyle = "#8fa9c2";
      ctx.fillText(item[1], lx + 16, legendY);
      lx += 16 + ctx.measureText(item[1]).width + 22;
    });
  }

  /* -- Sortable tables ------------------------------------------------
   * Clicking a <th> sorts the table body by that column. Numbers
   * (including $ values) sort numerically, everything else
   * alphabetically. The header shows an arrow for the sort.
   */
  function numValue(text) {
    var cleaned = text.replace(/[$,%]/g, "").replace(/,/g, "").trim();
    var n = parseFloat(cleaned);
    return isNaN(n) ? null : n;
  }
  function sortTable(table, colIndex, ascending) {
    var tbody = table.tBodies[0];
    if (!tbody) { return; }
    var rows = Array.prototype.slice.call(tbody.rows);
    rows.sort(function (a, b) {
      var ta = a.cells[colIndex].textContent.trim();
      var tb = b.cells[colIndex].textContent.trim();
      var na = numValue(ta);
      var nb = numValue(tb);
      var cmp;
      if (na !== null && nb !== null) {
        cmp = na - nb;
      } else {
        cmp = ta.localeCompare(tb);
      }
      return ascending ? cmp : -cmp;
    });
    rows.forEach(function (row) { tbody.appendChild(row); });
  }
  function wireSortableTables() {
    var tables = document.querySelectorAll("table.sortable");
    tables.forEach(function (table) {
      var headers = table.querySelectorAll("thead th");
      headers.forEach(function (th, idx) {
        th.addEventListener("click", function () {
          var ascending = th.dataset.sortDir !== "asc";
          headers.forEach(function (h) {
            h.dataset.sortDir = "";
            h.textContent = h.textContent.replace(/ [▲▼]$/, "");
          });
          th.dataset.sortDir = ascending ? "asc" : "desc";
          th.textContent += ascending ? " ▲" : " ▼";
          sortTable(table, idx, ascending);
        });
      });
    });
  }

  /* -- Command chips ---------------------------------------------------
   * Clicking a chip on the chat page puts its command into the
   * question box so the owner can edit it before asking.
   */
  function wireCommandChips() {
    var chips = document.querySelectorAll(".cmd-chip");
    chips.forEach(function (chip) {
      chip.addEventListener("click", function () {
        var input = document.querySelector("form input[name='q']");
        if (input) {
          input.value = chip.dataset.command || chip.textContent;
          input.focus();
        }
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("canvas.dcf-chart").forEach(drawDcfChart);
    wireSortableTables();
    wireCommandChips();
  });
})();
