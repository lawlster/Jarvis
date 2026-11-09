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

/* Step 56 — Browser voice (Web Speech API, Chrome/Edge).
 *
 * Talk to Jarvis and hear it reply, as close to the movie as a
 * browser allows: microphone input via SpeechRecognition and
 * spoken replies via speechSynthesis. Free, no key, no cloud
 * service, no audio ever leaves the browser — recognition and
 * synthesis both run locally in the owner's browser.
 *
 * Honest limits, stated here and on the page:
 * - No wake word: browsers do not allow background always-on
 *   listening, so Jarvis only listens after the owner clicks
 *   the microphone (or switches on hands-free, which resumes
 *   listening after each spoken reply).
 * - Voice is OFF by default (the owner's stored text
 *   preference) and listening only ever starts from a user
 *   gesture — clicking the microphone — or from the end of a
 *   spoken reply while hands-free is on.
 * - The voice is the browser's built-in voice, not a Hollywood
 *   actor; recognition accuracy depends on the microphone and
 *   accent. Language: en-SG first, en-US if the browser does
 *   not support en-SG.
 * - Only works while the chat page is open; each chat turn is
 *   a page reload, so voice state (on/off, hands-free, chosen
 *   voice) is kept in localStorage, never on the server.
 */
(function () {
  "use strict";

  var LS_ON = "jarvis.voice.on";
  var LS_HANDS_FREE = "jarvis.voice.handsfree";
  var LS_VOICE_NAME = "jarvis.voice.name";

  function storeGet(key) {
    try { return window.localStorage.getItem(key); } catch (e) { return null; }
  }
  function storeSet(key, value) {
    try { window.localStorage.setItem(key, value); } catch (e) { /* private mode */ }
  }

  /* -- Plain text for speaking --------------------------------------
   * The reply is plain text already, but strip anything that
   * should never be spoken: links, markdown marks, and command
   * chips the owner may have pasted in. Collapse whitespace.
   */
  function stripForSpeech(text) {
    return (text || "")
      .replace(/https?:\/\/\S+/g, " ")
      .replace(/[*#`_>]+/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  /* -- Reply-voice preference ---------------------------------------
   * Closest to the movie's Jarvis first: a British male voice.
   * Fallback order (documented, so the owner can see why a
   * voice was picked): 1. "Google UK English Male" (Chrome),
   * 2. "Daniel" (Apple's en-GB male voice), 3. any en-GB voice
   * whose name says Male, 4. any en-GB voice, 5. any English
   * voice, 6. the browser default (leave unset).
   */
  function pickVoice(voices) {
    var i, v;
    function byName(fragment) {
      for (i = 0; i < voices.length; i++) {
        if (voices[i].name.indexOf(fragment) !== -1) { return voices[i]; }
      }
      return null;
    }
    v = byName("Google UK English Male");
    if (v) { return v; }
    v = byName("Daniel");
    if (v) { return v; }
    for (i = 0; i < voices.length; i++) {
      if (voices[i].lang === "en-GB" &&
          voices[i].name.indexOf("Male") !== -1) { return voices[i]; }
    }
    for (i = 0; i < voices.length; i++) {
      if (voices[i].lang && voices[i].lang.indexOf("en-GB") === 0) {
        return voices[i];
      }
    }
    for (i = 0; i < voices.length; i++) {
      if (voices[i].lang && voices[i].lang.indexOf("en") === 0) {
        return voices[i];
      }
    }
    return null;
  }

  function wireVoice() {
    var panel = document.getElementById("voice-panel");
    if (!panel) { return; }
    var micBtn = document.getElementById("voice-mic");
    var toggle = document.getElementById("voice-toggle");
    var handsFreeBox = document.getElementById("voice-handsfree");
    var picker = document.getElementById("voice-picker");
    var statusEl = document.getElementById("voice-status");
    var input = document.querySelector("form input[name='q']");
    var form = input ? input.form : null;

    var hasRecognition = ("SpeechRecognition" in window) ||
      ("webkitSpeechRecognition" in window);
    var hasSynthesis = ("speechSynthesis" in window);
    var voiceOn = storeGet(LS_ON) === "on";
    var handsFree = storeGet(LS_HANDS_FREE) === "on";
    var recognition = null;
    var listening = false;
    var speaking = false;
    var triedUS = false;

    function setStatus(text) {
      if (statusEl) { statusEl.textContent = text; }
    }
    function setMicState() {
      if (!micBtn) { return; }
      micBtn.classList.toggle("listening", listening);
      micBtn.classList.toggle("speaking", speaking && !listening);
      micBtn.textContent = listening ? "◉ Listening…" :
        (speaking ? "🔊 Speaking…" : "🎙 Talk");
    }

    /* -- Spoken replies -------------------------------------------- */
    function populateVoices() {
      if (!hasSynthesis || !picker) { return; }
      var voices = window.speechSynthesis.getVoices();
      var preferred = pickVoice(voices);
      var stored = storeGet(LS_VOICE_NAME);
      var selected = null;
      picker.innerHTML = "";
      voices.forEach(function (voice) {
        var option = document.createElement("option");
        option.value = voice.name;
        option.textContent = voice.name + " (" + voice.lang + ")";
        picker.appendChild(option);
        if (stored && voice.name === stored) { selected = voice; }
      });
      if (!selected) { selected = preferred; }
      if (selected) { picker.value = selected.name; }
    }
    function chosenVoice() {
      if (!hasSynthesis) { return null; }
      var voices = window.speechSynthesis.getVoices();
      var stored = storeGet(LS_VOICE_NAME);
      var i;
      if (stored) {
        for (i = 0; i < voices.length; i++) {
          if (voices[i].name === stored) { return voices[i]; }
        }
      }
      return pickVoice(voices);
    }
    function speakReply() {
      if (!voiceOn || !hasSynthesis) { return; }
      var replyEl = document.getElementById("jarvis-reply-text");
      if (!replyEl) { return; }
      var text = stripForSpeech(replyEl.textContent);
      if (!text) { return; }
      window.speechSynthesis.cancel();
      var utterance = new SpeechSynthesisUtterance(text);
      utterance.rate = 0.95;
      utterance.pitch = 1.0;
      var voice = chosenVoice();
      if (voice) { utterance.voice = voice; }
      utterance.onstart = function () {
        speaking = true;
        setStatus("Speaking…");
        setMicState();
      };
      utterance.onend = function () {
        speaking = false;
        setStatus(voiceOn ? "Voice on — click the microphone to talk." : "Voice off.");
        setMicState();
        if (voiceOn && handsFree) { startListening(); }
      };
      utterance.onerror = function () {
        speaking = false;
        setStatus("Spoken reply failed — the text reply is above.");
        setMicState();
      };
      window.speechSynthesis.speak(utterance);
    }

    /* -- Microphone input ------------------------------------------ */
    function newRecognition() {
      var Ctor = window.SpeechRecognition || window.webkitSpeechRecognition;
      var rec = new Ctor();
      rec.lang = "en-SG";
      rec.interimResults = true;
      rec.maxAlternatives = 1;
      rec.onresult = function (event) {
        var interim = "";
        var finalText = "";
        var i, result;
        for (i = event.resultIndex; i < event.results.length; i++) {
          result = event.results[i];
          if (result.isFinal) {
            finalText += result[0].transcript;
          } else {
            interim += result[0].transcript;
          }
        }
        if (input) {
          input.value = finalText || interim;
        }
        if (finalText && form) {
          form.submit();
        }
      };
      rec.onend = function () {
        listening = false;
        setStatus(voiceOn ? "Voice on — click the microphone to talk." : "Voice off.");
        setMicState();
        if (voiceOn && handsFree && input && !input.value) {
          startListening();
        }
      };
      rec.onerror = function (event) {
        listening = false;
        setMicState();
        if (event.error === "language-not-supported" && !triedUS) {
          triedUS = true;
          setStatus("en-SG is not supported here — switched to en-US.");
          rec.lang = "en-US";
          startListening();
          return;
        }
        if (event.error === "not-allowed") {
          setStatus("Microphone access was denied — allow it in the browser address bar and try again.");
        } else if (event.error === "no-speech") {
          setStatus("Nothing heard — click the microphone and try again.");
        } else {
          setStatus("Voice input stopped (" + event.error + ").");
        }
      };
      return rec;
    }
    function startListening() {
      if (!voiceOn || !hasRecognition || listening) { return; }
      if (hasSynthesis) { window.speechSynthesis.cancel(); }
      if (!recognition) { recognition = newRecognition(); }
      try {
        recognition.start();
      } catch (e) {
        setStatus("Could not start listening — click the microphone to try again.");
        return;
      }
      listening = true;
      setStatus("Listening…");
      setMicState();
    }
    function stopListening() {
      if (recognition && listening) {
        try { recognition.stop(); } catch (e) { /* already stopped */ }
      }
      listening = false;
      setMicState();
    }

    /* -- Controls --------------------------------------------------- */
    function applyVoiceOn() {
      if (micBtn) { micBtn.disabled = !voiceOn || !hasRecognition; }
      if (handsFreeBox) { handsFreeBox.disabled = !voiceOn; }
      if (picker) { picker.disabled = !voiceOn || !hasSynthesis; }
      if (!voiceOn) {
        stopListening();
        if (hasSynthesis) { window.speechSynthesis.cancel(); }
        speaking = false;
        setStatus("Voice off.");
      } else if (!hasRecognition) {
        setStatus("Voice input is not supported in this browser — try Chrome or Edge.");
      } else if (!hasSynthesis) {
        setStatus("Voice on — microphone ready. Spoken replies are not supported in this browser — try Chrome or Edge.");
      } else {
        setStatus("Voice on — click the microphone to talk.");
      }
      setMicState();
    }
    if (toggle) {
      toggle.checked = voiceOn;
      toggle.addEventListener("change", function () {
        voiceOn = toggle.checked;
        storeSet(LS_ON, voiceOn ? "on" : "off");
        applyVoiceOn();
      });
    }
    if (handsFreeBox) {
      handsFreeBox.checked = handsFree;
      handsFreeBox.addEventListener("change", function () {
        handsFree = handsFreeBox.checked;
        storeSet(LS_HANDS_FREE, handsFree ? "on" : "off");
      });
    }
    if (picker) {
      picker.addEventListener("change", function () {
        storeSet(LS_VOICE_NAME, picker.value);
      });
    }
    if (micBtn) {
      micBtn.addEventListener("click", function () {
        if (!voiceOn) { return; }
        if (listening) {
          stopListening();
        } else {
          startListening();
        }
      });
    }
    if (hasSynthesis) {
      populateVoices();
      if ("onvoiceschanged" in window.speechSynthesis) {
        window.speechSynthesis.onvoiceschanged = populateVoices;
      }
    }
    applyVoiceOn();
    // After a spoken turn the page reloads with the reply: speak
    // it when voice is on. Listening never starts here — only a
    // microphone click (or a spoken reply ending with hands-free
    // on) starts it.
    speakReply();
  }

  document.addEventListener("DOMContentLoaded", function () {
    wireVoice();
  });
})();
