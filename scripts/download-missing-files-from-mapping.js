/** * Wolf Surgical Web Downloader v1.0 - Phase 3 Executor
 * Ingests the 'exact-missing-1.0' payload and triggers individual file downloads.
 */
(async function () {
  console.log("Wolf Surgical Web Downloader v1.0 Initiated...");

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
  panel.style.border = "2px solid #e53935"; // Action Red
  panel.style.borderRadius = "8px";
  panel.style.fontFamily = "monospace";
  panel.style.minWidth = "320px";

  panel.innerHTML = `
        <h3 style="margin:0 0 10px 0; color:#ef5350;">Wolf Surgical Downloader</h3>
        <p style="font-size:11px; color:#ffcc80; margin: 0 0 10px 0;">CRITICAL: Tab must remain in foreground. You MUST allow "Multiple Downloads" in browser URL bar when prompted.</p>
        <label style="font-size:12px;">Load Payload (exact-missing.json):</label><br>
        <input type="file" id="wolf-file-input" accept=".json" style="width:100%; margin:5px 0 15px; padding:5px;"><br>
        <label style="font-size:12px;">File Cooldown (seconds):</label><br>
        <input type="number" id="wolf-cooldown" value="5" min="2" style="width:100%; margin:5px 0 15px; padding:5px;"><br>
        <button id="wolf-start" disabled style="width:100%; padding:10px; background:#9e9e9e; color:white; border:none; cursor:not-allowed; font-weight:bold;">Awaiting Payload...</button>
        <div id="wolf-status" style="margin-top:15px; font-size:12px; color:#b0bec5;">Ready.</div>
    `;
  document.body.appendChild(panel);

  const statusEl = document.getElementById("wolf-status");
  const startBtn = document.getElementById("wolf-start");
  const fileInput = document.getElementById("wolf-file-input");

  let payloadData = null;

  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const updateStatus = (msg) => { console.log(`[DOWNLOADER] ${msg}`); statusEl.innerText = msg; };

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

  async function seekAndClickFolder(title) {
    let scrollingEl = document.scrollingElement || document.body;
    let lastHeight = scrollingEl.scrollHeight;
    let idleTicks = 0;

    while (idleTicks < CONFIG.MAX_IDLE_TICKS) {
      let target = Array.from(document.querySelectorAll("a.fldr")).find(el => el.title === title || el.innerText.trim() === title);

      if (target) {
        target.scrollIntoView({ block: 'center' });
        await sleep(400);

        target = Array.from(document.querySelectorAll("a.fldr")).find(el => el.title === title || el.innerText.trim() === title);
        if (!target) continue;

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
        updateStatus("Loading directory contents into DOM...");
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
        if (!json.dirs || !Array.isArray(json.dirs)) throw new Error("Invalid format. Expected 'dirs' array.");
        payloadData = json;
        startBtn.disabled = false;
        startBtn.style.backgroundColor = "#e53935";
        startBtn.style.cursor = "pointer";

        let totalFiles = json.dirs.reduce((sum, dir) => sum + (dir.missingFiles ? dir.missingFiles.length : 0), 0);
        startBtn.innerText = `Download ${totalFiles} Files (${json.dirs.length} Folders)`;
      } catch (err) {
        updateStatus(`Error parsing JSON: ${err.message}`);
      }
    };
    reader.readAsText(file);
  });

  // --- MAIN EXECUTION LOOP ---
  startBtn.onclick = async function () {
    const cooldownMs = parseInt(document.getElementById("wolf-cooldown").value, 10) * 1000;

    startBtn.disabled = true;
    startBtn.style.backgroundColor = "#9e9e9e";
    fileInput.disabled = true;

    // Pre-Flight Shortest Path Optimization
    const queue = payloadData.dirs.sort((a, b) => {
      if (a.absolutePath < b.absolutePath) return -1;
      if (a.absolutePath > b.absolutePath) return 1;
      return 0;
    });

    const basePath = payloadData.basePath || "";
    let totalSuccess = 0;
    let totalFailed = 0;

    for (let i = 0; i < queue.length; i++) {
      let dir = queue[i];
      if (!dir.missingFiles || dir.missingFiles.length === 0) continue;

      updateStatus(`[DIR ${i + 1}/${queue.length}] Routing to: ${dir.absolutePath}`);

      try {
        let fullPathArray = parsePathToArray(basePath, dir.absolutePath);

        await navigateTo(fullPathArray);
        await scrollCurrentViewToBottom();

        // Build DOM Node Map for instant targeting
        const rows = [...document.querySelectorAll('#file_list_container > li')];
        let fileNodeMap = {};

        for (let row of rows) {
          let isFolder = row.querySelector("a.fldr") !== null;
          if (isFolder) continue;

          let nameEl = row.querySelector('.file_name a') || row.querySelector('.file_name span');
          if (nameEl) {
            let fileName = nameEl.getAttribute('title') || nameEl.innerText.trim();
            fileNodeMap[fileName] = row;
          }
        }

        // Execute surgical downloads
        for (let f = 0; f < dir.missingFiles.length; f++) {
          let targetFile = dir.missingFiles[f];
          updateStatus(`[DIR ${i + 1}/${queue.length}] Triggering [${f + 1}/${dir.missingFiles.length}]: ${targetFile}`);

          let targetRow = fileNodeMap[targetFile];
          if (!targetRow) {
            console.warn(`[DOWNLOADER] File not found in DOM: ${targetFile}`);
            totalFailed++;
            continue;
          }

          // 1. Snap into view
          targetRow.scrollIntoView({ block: 'center' });
          await sleep(400);

          // 2. Open Context Menu
          let menuTrigger = targetRow.querySelector('.file_menu_list a img') || targetRow.querySelector('.file_menu_list a');
          if (!menuTrigger) {
            totalFailed++;
            continue;
          }

          forceClick(menuTrigger);
          await sleep(400);

          // 3. Trigger Download
          let downloadBtn = document.querySelector('ul#content.menu_inside .file_menu_download');
          if (downloadBtn && window.getComputedStyle(downloadBtn).display !== 'none') {
            let parentLi = downloadBtn.closest('li');
            if (parentLi) parentLi.classList.add('hover');
            downloadBtn.classList.add('hover');

            forceClick(downloadBtn);
            totalSuccess++;

            // 4. Purge UI State
            await sleep(300);
            forceClick(document.body);

            // 5. Rate-Limit Cooldown
            await sleep(cooldownMs);
          } else {
            console.warn(`[DOWNLOADER] Download option disabled/hidden for: ${targetFile}`);
            forceClick(document.body);
            totalFailed++;
          }
        }

      } catch (err) {
        console.error(`[DOWNLOADER ERROR] Failed processing directory ${dir.absolutePath}:`, err);
        totalFailed += dir.missingFiles.length; // Count all files in dir as failed
      }
    }

    // --- COMPLETION ---
    panel.style.backgroundColor = "#1b5e20";
    updateStatus(`Extraction Complete. Success: ${totalSuccess} | Failed: ${totalFailed}`);
    startBtn.innerText = "✅ Queue Exhausted";
  };
})();
