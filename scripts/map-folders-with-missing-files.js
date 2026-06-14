/** * Wolf Web File Mapper v1.2 - Hardened Navigation Edition
 * Ingests a folder-level payload and extracts a strict list of exact files within those folders.
 */
(async function () {
  console.log("Wolf Web File Mapper v1.2 Initiated...");

  const CONFIG = {
    PAGE_LOAD_WAIT: 3500,
    SCROLL_INTERVAL: 1000,
    MAX_IDLE_TICKS: 15
  };

  // --- 1. BUILD UI ---
  const panel = document.createElement("div");
  panel.style.position = "fixed";
  panel.style.bottom = "20px";
  panel.style.right = "20px";
  panel.style.zIndex = "999999";
  panel.style.padding = "20px";
  panel.style.backgroundColor = "#263238";
  panel.style.color = "white";
  panel.style.border = "2px solid #29b6f6";
  panel.style.borderRadius = "8px";
  panel.style.fontFamily = "monospace";
  panel.style.minWidth = "320px";

  panel.innerHTML = `
        <h3 style="margin:0 0 10px 0; color:#29b6f6;">Wolf File Mapper v1.2</h3>
        <p style="font-size:11px; color:#ffcc80; margin: 0 0 10px 0;">CRITICAL: Keep this browser tab in the foreground to prevent engine throttling.</p>
        <label style="font-size:12px;">Load Payload (Partial/Missing):</label><br>
        <input type="file" id="wolf-file-input" accept=".json" style="width:100%; margin:5px 0 15px; padding:5px;"><br>
        <button id="wolf-start" disabled style="width:100%; padding:10px; background:#9e9e9e; color:white; border:none; cursor:not-allowed; font-weight:bold;">Awaiting Payload...</button>
        <div id="wolf-status" style="margin-top:15px; font-size:12px; color:#b0bec5;">Ready.</div>
    `;
  document.body.appendChild(panel);

  const statusEl = document.getElementById("wolf-status");
  const startBtn = document.getElementById("wolf-start");
  const fileInput = document.getElementById("wolf-file-input");

  let payloadData = null;

  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const updateStatus = (msg) => { console.log(`[MAPPER] ${msg}`); statusEl.innerText = msg; };

  // Hardened Click Synthesizer
  const forceClick = (el) => {
    if (!el) return;
    ['mouseenter', 'mouseover', 'mousedown', 'mouseup', 'click'].forEach(evt => {
      el.dispatchEvent(new MouseEvent(evt, { view: window, bubbles: true, cancelable: true, buttons: 1 }));
    });
  };

  function getCurrentBreadcrumbs() {
    try {
      return [...document.querySelector("div.breadcrumb").childNodes]
        .filter(n => n.nodeType === 1).map(el => el.title).filter(t => t);
    } catch (e) { return []; }
  }

  function clickBreadcrumb(title) {
    let nodes = [...document.querySelector("div.breadcrumb").childNodes].filter(n => n.nodeType === 1);
    let target = nodes.find(el => el.title === title);
    if (target) {
      forceClick(target);
      return true;
    }
    return false;
  }

  // --- UPGRADED: Hardened Seeker ---
  async function seekAndClickFolder(title) {
    let scrollingEl = document.scrollingElement || document.body;
    let lastHeight = scrollingEl.scrollHeight;
    let idleTicks = 0;

    while (idleTicks < CONFIG.MAX_IDLE_TICKS) {
      let target = Array.from(document.querySelectorAll("a.fldr")).find(el => el.title === title || el.innerText.trim() === title);

      if (target) {
        // FIX 1: Instant snap to prevent animation race conditions
        target.scrollIntoView({ block: 'center' });

        // FIX 2: Layout shift settlement buffer
        await sleep(400);

        // Ensure element wasn't detached during shift
        target = Array.from(document.querySelectorAll("a.fldr")).find(el => el.title === title || el.innerText.trim() === title);
        if (!target) continue;

        // FIX 3: Robust click
        forceClick(target);
        return true;
      }

      updateStatus(`Seeking '${title}'...`);
      window.scrollBy(0, 800);
      await sleep(CONFIG.SCROLL_INTERVAL);

      if (scrollingEl.scrollHeight > lastHeight) {
        lastHeight = scrollingEl.scrollHeight;
        idleTicks = 0;
      } else {
        if (Math.ceil(scrollingEl.scrollTop + window.innerHeight) >= scrollingEl.scrollHeight - 10) {
          idleTicks++;
        }
      }
    }
    return false;
  }

  async function scrollCurrentViewToBottom() {
    let scrollingEl = document.scrollingElement || document.body;
    let lastHeight = scrollingEl.scrollHeight;
    let idleTicks = 0;

    while (idleTicks < 5) {
      window.scrollBy(0, 800);
      await sleep(CONFIG.SCROLL_INTERVAL);

      if (scrollingEl.scrollHeight > lastHeight) {
        lastHeight = scrollingEl.scrollHeight;
        idleTicks = 0;
        updateStatus("Loading directory contents...");
      } else {
        if (Math.ceil(scrollingEl.scrollTop + window.innerHeight) >= scrollingEl.scrollHeight - 10) {
          idleTicks++;
        }
      }
    }
  }

  function parsePathToArray(basePath, absolutePath) {
    let cleanPath = absolutePath.replace(/^\//, '');
    return cleanPath.split('/').filter(p => p);
  }

  async function navigateTo(targetPathArray) {
    let currentPath = getCurrentBreadcrumbs();
    let matchIndex = -1;
    for (let i = 0; i < Math.min(currentPath.length, targetPathArray.length); i++) {
      if (currentPath[i] === targetPathArray[i]) matchIndex = i;
      else break;
    }

    let commonAncestor = matchIndex >= 0 ? currentPath[matchIndex] : null;

    if (currentPath.join('/') !== targetPathArray.slice(0, currentPath.length).join('/')) {
      if (commonAncestor) {
        updateStatus(`Traversing UP to: ${commonAncestor}`);
        if (clickBreadcrumb(commonAncestor)) await sleep(CONFIG.PAGE_LOAD_WAIT);
        else throw new Error(`Breadcrumb missing: ${commonAncestor}`);
      }
    }

    currentPath = getCurrentBreadcrumbs();
    let startIndex = currentPath.length;

    for (let i = startIndex; i < targetPathArray.length; i++) {
      let nextFolder = targetPathArray[i];
      updateStatus(`Traversing DOWN into: ${nextFolder}`);

      let found = await seekAndClickFolder(nextFolder);
      if (found) {
        await sleep(CONFIG.PAGE_LOAD_WAIT);
      } else {
        throw new Error(`Folder missing from DOM after deep seek: ${nextFolder}`);
      }
    }
  }

  // --- EVENT LISTENERS ---
  fileInput.addEventListener('change', function (e) {
    const file = e.target.files[0];
    if (!file) return;

    const reader = new FileReader();
    reader.onload = function (e) {
      try {
        const json = JSON.parse(e.target.result);
        if (!json.dirs || !Array.isArray(json.dirs)) throw new Error("Invalid format.");
        payloadData = json;
        startBtn.disabled = false;
        startBtn.style.backgroundColor = "#0288d1";
        startBtn.style.cursor = "pointer";
        startBtn.innerText = `Start File Mapping (${json.dirs.length} folders)`;
      } catch (err) {
        updateStatus(`Error parsing JSON: ${err.message}`);
      }
    };
    reader.readAsText(file);
  });

  // --- MAIN EXECUTION LOOP ---
  startBtn.onclick = async function () {
    startBtn.disabled = true;
    startBtn.style.backgroundColor = "#9e9e9e";
    fileInput.disabled = true;

    const queue = payloadData.dirs.sort((a, b) => {
      if (a.absolutePath < b.absolutePath) return -1;
      if (a.absolutePath > b.absolutePath) return 1;
      return 0;
    });

    updateStatus(`Queue optimized for shortest-path traversal.`);

    const basePath = payloadData.basePath || "";
    let mappedResults = [];

    for (let i = 0; i < queue.length; i++) {
      let item = queue[i];
      updateStatus(`[${i + 1}/${queue.length}] Mapping: ${item.title}`);

      try {
        let fullPathArray = parsePathToArray(basePath, item.absolutePath);

        await navigateTo(fullPathArray);
        await scrollCurrentViewToBottom();

        const rows = [...document.querySelectorAll('#file_list_container > li')];
        let fileList = [];

        for (let row of rows) {
          let isFolder = row.querySelector("a.fldr") !== null;
          if (isFolder) continue;

          let nameEl = row.querySelector('.file_name a') || row.querySelector('.file_name span');
          if (!nameEl) continue;

          let fileName = nameEl.getAttribute('title') || nameEl.innerText.trim();
          let sizeEl = row.querySelector('.file_size');
          let dateEl = row.querySelector('.file_date');

          fileList.push({
            fileName: fileName,
            serverSize: sizeEl ? sizeEl.innerText.trim() : null,
            serverModified: dateEl ? dateEl.innerText.trim() : null
          });
        }

        mappedResults.push({
          absolutePath: item.absolutePath,
          href: item.href,
          totalFilesFound: fileList.length,
          files: fileList
        });

      } catch (err) {
        console.error(`[MAPPER ERROR] Failed at ${item.title}:`, err);
        mappedResults.push({
          absolutePath: item.absolutePath,
          href: item.href,
          error: err.message
        });
      }
    }

    updateStatus("Mapping Complete. Generating Payload.");
    panel.style.backgroundColor = "#1b5e20";

    const finalPayload = JSON.stringify({
      version: "file-map-1.0",
      basePath: basePath,
      mappedFolders: mappedResults
    }, null, 2);

    const blob = new Blob([finalPayload], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `file_map_export_${Date.now()}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);

    startBtn.innerText = "✅ Mapping Finished";
  };
})();
