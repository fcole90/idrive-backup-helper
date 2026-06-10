/** * Wolf SPA Crawler v0.3.0 - Deep Extraction Protocol
 * Run this on https://www.idrive.com/idrive/home/<YOUR_DEVICE_ID>/<VOLUME_ID>/
 */
(async function () {
  console.log("Wolf SPA Crawler v0.3.0 Initiated...");

  const CONFIG = {
    SCROLL_INTERVAL: 1500,
    PAGE_LOAD_WAIT: 3500,
    TIMEOUT_MS: 15000
  };

  // --- 1. BUILD CONTROL PANEL UI ---
  const panel = document.createElement("div");
  panel.style.position = "fixed";
  panel.style.bottom = "20px";
  panel.style.right = "20px";
  panel.style.zIndex = "999999";
  panel.style.padding = "20px";
  panel.style.backgroundColor = "#263238";
  panel.style.color = "white";
  panel.style.border = "2px solid #546e7a";
  panel.style.borderRadius = "8px";
  panel.style.boxShadow = "0 8px 16px rgba(0,0,0,0.7)";
  panel.style.fontFamily = "monospace";
  panel.style.minWidth = "300px";

  panel.innerHTML = `
        <h3 style="margin:0 0 10px 0; color:#ff9800;">Wolf Crawler v0.3.0</h3>
        <label>Search Depth (0 = current only):</label><br>
        <input type="number" id="wolf-depth" value="1" min="0" max="5" style="width:100%; margin:5px 0 15px; padding:5px;"><br>
        <button id="wolf-start" style="width:100%; padding:10px; background:#4caf50; color:white; border:none; cursor:pointer; font-weight:bold;">Start Crawl</button>
        <div id="wolf-status" style="margin-top:15px; font-size:12px; color:#b0bec5;">Waiting for command...</div>
    `;
  document.body.appendChild(panel);

  const statusEl = document.getElementById("wolf-status");
  const startBtn = document.getElementById("wolf-start");

  // --- 2. CRAWLER HELPERS ---

  const sleep = (ms) => new Promise(r => setTimeout(r, ms));

  const updateStatus = (msg) => {
    console.log(`[CRAWLER] ${msg}`);
    statusEl.innerText = msg;
  };

  // Synthesize human mouse interactions to bypass jQuery traps
  const forceClick = (el) => {
    ['mouseenter', 'mouseover', 'mousedown', 'mouseup', 'click'].forEach(evt => {
      el.dispatchEvent(new MouseEvent(evt, { view: window, bubbles: true, cancelable: true, buttons: 1 }));
    });
  };

  async function scrollCurrentView() {
    let scrollingEl = document.scrollingElement || document.body;
    let lastHeight = scrollingEl.scrollHeight;
    let lastChange = Date.now();

    while (Date.now() - lastChange < CONFIG.TIMEOUT_MS) {
      window.scrollTo(0, scrollingEl.scrollHeight);
      await sleep(CONFIG.SCROLL_INTERVAL);
      if (scrollingEl.scrollHeight > lastHeight) {
        lastHeight = scrollingEl.scrollHeight;
        lastChange = Date.now();
        updateStatus("Scrolling: Fetching new files...");
      } else {
        updateStatus(`Scrolling: Checking if loading... (${Math.round((CONFIG.TIMEOUT_MS - (Date.now() - lastChange)) / 1000)}s)`);
      }
    }
  }

  function getCurrentBreadcrumbs() {
    try {
      return [...document.querySelector("div.breadcrumb").childNodes]
        .filter(n => n.nodeType === 1).map(el => el.title).filter(t => t);
    } catch (e) { return []; }
  }

  function clickBreadcrumb(title) {
    let nodes = [...document.querySelector("div.breadcrumb").childNodes].filter(n => n.nodeType === 1);
    let target = nodes.find(el => el.title === title);
    if (target) { target.click(); return true; }
    return false;
  }

  function clickFolder(title) {
    let target = Array.from(document.querySelectorAll("a.fldr")).find(el => el.title === title);
    if (target) { target.click(); return true; }
    return false;
  }

  // --- 3. THE NAVIGATION ENGINE ---
  async function navigateTo(targetPathArray) {
    updateStatus(`Routing to: /${targetPathArray.join('/')}`);
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
        else throw new Error("Breadcrumb not found");
      }
    }

    currentPath = getCurrentBreadcrumbs();
    let startIndex = currentPath.length;

    for (let i = startIndex; i < targetPathArray.length; i++) {
      let nextFolder = targetPathArray[i];
      updateStatus(`Traversing DOWN into: ${nextFolder}`);

      await sleep(1000);
      if (clickFolder(nextFolder)) {
        await sleep(CONFIG.PAGE_LOAD_WAIT);
      } else {
        window.scrollTo(0, document.scrollingElement.scrollHeight);
        await sleep(2000);
        if (clickFolder(nextFolder)) await sleep(CONFIG.PAGE_LOAD_WAIT);
        else throw new Error(`Folder missing from DOM: ${nextFolder}`);
      }
    }
  }

  // --- 4. MAIN CRAWL LOOP ---
  startBtn.onclick = async function () {
    const maxDepth = parseInt(document.getElementById("wolf-depth").value, 10);
    startBtn.disabled = true;
    startBtn.style.backgroundColor = "#9e9e9e";

    let rootPathArray = getCurrentBreadcrumbs();
    let queue = [{ pathArray: rootPathArray, depth: 0 }];
    let results = [];
    let visited = new Set();

    while (queue.length > 0) {
      let currentTask = queue.shift();
      let pathString = currentTask.pathArray.join('/');

      if (visited.has(pathString)) continue;
      visited.add(pathString);

      updateStatus(`Processing Directory: /${pathString}`);

      try {
        await navigateTo(currentTask.pathArray);
        await scrollCurrentView();

        // 3. EXTRACT FOLDERS AND DETAILS (Synchronous UI interaction loop)
        const rows = [...document.querySelectorAll('#file_list_container > li')];
        let currentPathForJson = currentTask.pathArray.join('/');
        if (currentPathForJson && !currentPathForJson.startsWith('/')) {
          currentPathForJson = '/' + currentPathForJson;
        }

        for (let i = 0; i < rows.length; i++) {
          let row = rows[i];
          let anchor = row.querySelector("a.fldr");
          if (!anchor) continue;

          let title = anchor.getAttribute('title') || anchor.innerText.trim();
          updateStatus(`Extracting details [${i + 1}/${rows.length}]: ${title}`);

          let fullTarget = currentPathForJson ? `${currentPathForJson}/${title}` : title;
          fullTarget = fullTarget.replace(/\/\//g, '/');

          let folderSize = null, fileCount = null, modifiedDate = null;

          // Trigger context menu
          let menuBtn = row.querySelector('.file_menu_list a');
          if (menuBtn) {
            forceClick(menuBtn);
            await sleep(400);

            // Trigger details modal
            let detailsBtn = document.querySelector('ul#content.menu_inside .file_menu_details');
            if (detailsBtn) {
              let parentLi = detailsBtn.closest('li');
              if (parentLi) parentLi.classList.add('hover');
              detailsBtn.classList.add('hover');

              forceClick(detailsBtn);

              // Wait for modal visibility
              let modalVisible = false;
              for (let wait = 0; wait < 15; wait++) {
                await sleep(200);
                let modal = document.querySelector('.details_container.popup_container');
                if (modal && window.getComputedStyle(modal).display !== 'none') {
                  modalVisible = true;
                  break;
                }
              }

              if (modalVisible) {
                // Poll for server data
                let sizeEl, countEl, dateEl;
                let isPopulated = false;
                for (let attempts = 0; attempts < 20; attempts++) {
                  await sleep(500);
                  sizeEl = document.getElementById('folderDetailsSize');
                  countEl = document.getElementById('folderDetailsCount');
                  dateEl = document.getElementById('folderDetailsLmd');

                  if (sizeEl && sizeEl.innerText.trim() !== '' && sizeEl.innerText.trim() !== '-') {
                    isPopulated = true;
                    break;
                  }
                }

                if (isPopulated) {
                  folderSize = sizeEl ? sizeEl.innerText.trim() : null;
                  fileCount = countEl ? countEl.innerText.trim() : null;
                  modifiedDate = dateEl ? dateEl.innerText.trim() : null;
                } else {
                  console.warn(`[CRAWLER] Timeout waiting for server data on ${title}`);
                }

                // Close modal
                let closeBtn = document.querySelector('.popup_close_btn');
                if (closeBtn) {
                  forceClick(closeBtn);
                  await sleep(500);
                }
              } else {
                console.warn(`[CRAWLER] Modal failed to open for ${title}`);
              }
            }
          }

          // Commit to payload
          results.push({
            href: anchor.href,
            title: title,
            absolutePath: fullTarget,
            depth: currentTask.depth + 1,
            folderSize: folderSize,
            fileCount: fileCount,
            modifiedDate: modifiedDate
          });

          // Enqueue for deeper crawl if limit permits
          if (currentTask.depth < maxDepth) {
            let newPathArray = [...currentTask.pathArray, title];
            queue.push({ pathArray: newPathArray, depth: currentTask.depth + 1 });
          }
        }

      } catch (err) {
        console.error(`[CRAWLER ERROR] Failed at /${pathString}:`, err);
        updateStatus(`Error navigating /${pathString}. Skipping.`);
      }
    }

    // --- 5. DOWNLOAD PROTOCOL ---
    updateStatus("Crawl Complete. Generating Payload.");
    panel.style.backgroundColor = "#1b5e20";

    // VERSION BUMP AND PAYLOAD GENERATION
    const payload = JSON.stringify({
      version: "0.3.0",
      basePath: '/' + rootPathArray.join('/'),
      totalDirectoriesFound: results.length,
      dirs: results
    }, null, 2);

    let rawPath = rootPathArray.length ? rootPathArray.join('_') : "IDrive_Deep_Export";
    const safeFileName = rawPath.replace(/[^a-z0-9_-]/gi, '_').replace(/_+/g, '_') + "_v0.3.0.json";

    const blob = new Blob([payload], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = safeFileName;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);

    startBtn.innerText = "✅ Extraction Finished";
  };
})();
