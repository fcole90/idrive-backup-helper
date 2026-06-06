/** 
 * Run this on https://www.idrive.com/idrive/home/<YOUR_DEVICE_ID>/<VOLUME_ID>/
 * and it will crawl through all folders up to the specified depth, auto-scrolling to load contents, 
 * and extract a JSON file with the folder structure and links.
 * 
 * You probably want to set the depth to 1.  That will give you all folders in the current view and one level down.
 * 
 * Setting it to 0 will only give you the folders in the current view, 
 * which is also useful if you have a smaller set of folders and want to avoid long crawl times.
 */
(async function () {
  console.log("Wolf SPA Crawler Initiated...");

  const CONFIG = {
    SCROLL_INTERVAL: 1500,    // Wait between scroll ticks
    PAGE_LOAD_WAIT: 3500,     // Network wait time after clicking a folder/breadcrumb
    TIMEOUT_MS: 15000         // Scroll timeout to assume folder is fully loaded
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
        <h3 style="margin:0 0 10px 0; color:#ff9800;">Wolf Crawler</h3>
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

  // Auto-scroll logic from previous version
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

  // Get current breadcrumb array (e.g., ['Home', 'Documents'])
  function getCurrentBreadcrumbs() {
    try {
      return [...document.querySelector("div.breadcrumb").childNodes]
        .filter(n => n.nodeType === 1) // Only clickable elements
        .map(el => el.title)
        .filter(t => t);
    } catch (e) {
      return [];
    }
  }

  // Click breadcrumb by name
  function clickBreadcrumb(title) {
    let nodes = [...document.querySelector("div.breadcrumb").childNodes].filter(n => n.nodeType === 1);
    let target = nodes.find(el => el.title === title);
    if (target) {
      target.click();
      return true;
    }
    return false;
  }

  // Click folder by name
  function clickFolder(title) {
    let target = Array.from(document.querySelectorAll("a.fldr")).find(el => el.title === title);
    if (target) {
      target.click();
      return true;
    }
    return false;
  }

  // --- 3. THE NAVIGATION ENGINE ---
  async function navigateTo(targetPathArray) {
    updateStatus(`Routing to: /${targetPathArray.join('/')}`);
    let currentPath = getCurrentBreadcrumbs();

    // Find Common Ancestor
    let matchIndex = -1;
    for (let i = 0; i < Math.min(currentPath.length, targetPathArray.length); i++) {
      if (currentPath[i] === targetPathArray[i]) {
        matchIndex = i;
      } else {
        break;
      }
    }

    let commonAncestor = matchIndex >= 0 ? currentPath[matchIndex] : null;

    // Route UP (Click Breadcrumb)
    if (currentPath.join('/') !== targetPathArray.slice(0, currentPath.length).join('/')) {
      if (commonAncestor) {
        updateStatus(`Traversing UP to: ${commonAncestor}`);
        if (clickBreadcrumb(commonAncestor)) {
          await sleep(CONFIG.PAGE_LOAD_WAIT);
        } else {
          throw new Error("Breadcrumb not found");
        }
      }
    }

    // Route DOWN (Click Folders)
    currentPath = getCurrentBreadcrumbs(); // Refresh after UP movement
    let startIndex = currentPath.length;

    for (let i = startIndex; i < targetPathArray.length; i++) {
      let nextFolder = targetPathArray[i];
      updateStatus(`Traversing DOWN into: ${nextFolder}`);

      // Wait to ensure DOM is ready, then find and click
      await sleep(1000);
      if (clickFolder(nextFolder)) {
        await sleep(CONFIG.PAGE_LOAD_WAIT);
      } else {
        // If folder isn't visible, scroll a bit and try again
        window.scrollTo(0, document.scrollingElement.scrollHeight);
        await sleep(2000);
        if (clickFolder(nextFolder)) {
          await sleep(CONFIG.PAGE_LOAD_WAIT);
        } else {
          throw new Error(`Folder missing from DOM: ${nextFolder}`);
        }
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
    let visited = new Set(); // Track to prevent infinite loops

    while (queue.length > 0) {
      let currentTask = queue.shift(); // Dequeue
      let pathString = currentTask.pathArray.join('/');

      if (visited.has(pathString)) continue;
      visited.add(pathString);

      updateStatus(`Processing [Queue: ${queue.length}]: /${pathString}`);

      try {
        // 1. Navigate to target
        await navigateTo(currentTask.pathArray);

        // 2. Scroll to load all contents
        updateStatus(`Loading view: /${pathString}`);
        await scrollCurrentView();

        // 3. Extract Folders
        const foldersInView = [...document.querySelectorAll("a.fldr")];
        let currentPathForJson = currentTask.pathArray.join('/');
        if (currentPathForJson && !currentPathForJson.startsWith('/')) {
          currentPathForJson = '/' + currentPathForJson;
        }

        foldersInView.forEach(el => {
          // Save to results
          let fullTarget = currentPathForJson ? `${currentPathForJson}/${el.title}` : el.title;
          fullTarget = fullTarget.replace(/\/\//g, '/');

          results.push({
            href: el.href,
            title: el.title,
            absolutePath: fullTarget,
            depth: currentTask.depth + 1
          });

          // If within depth limit, add to queue
          if (currentTask.depth < maxDepth) {
            let newPathArray = [...currentTask.pathArray, el.title];
            queue.push({ pathArray: newPathArray, depth: currentTask.depth + 1 });
          }
        });

      } catch (err) {
        console.error(`[CRAWLER ERROR] Failed at /${pathString}:`, err);
        updateStatus(`Error navigating /${pathString}. Skipping.`);
      }
    }

    // --- 5. DOWNLOAD PROTOCOL ---
    updateStatus("Crawl Complete. Generating Payload.");
    panel.style.backgroundColor = "#1b5e20"; // Success Green

    const payload = JSON.stringify({
      basePath: '/' + rootPathArray.join('/'),
      totalDirectoriesFound: results.length,
      dirs: results
    }, null, 2);

    let rawPath = rootPathArray.length ? rootPathArray.join('_') : "IDrive_Deep_Export";
    const safeFileName = rawPath.replace(/[^a-z0-9_-]/gi, '_').replace(/_+/g, '_') + ".json";

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
    // setTimeout(() => document.body.removeChild(panel), 4000);
  };
})();