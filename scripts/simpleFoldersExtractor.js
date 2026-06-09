/**
 * This script is designed for quick extraction of folder links from the current view in IDrive's web interface.
 * It auto-scrolls to ensure all folders are loaded, then compiles a JSON file of the folder structure.
 * 
 * In most cases use deepFolderExtractor.js instead, which will crawl through all folders up to a specified depth.  
 * This is a more lightweight version that only captures the folders currently visible in the UI.
 */
(async function () {
    console.log("Wolf Extractor Initiated...");


    // --- 1. CONFIGURATION ---
    const CONFIG = {
        TIMEOUT_MS: 20000,       // Time to wait for new content before concluding (20 seconds)
        SCROLL_INTERVAL: 2000    // Frequency of scroll checks (2 seconds)
    };

    // --- 2. INJECT THE OVERLAY BUTTON ---
    let btn = document.createElement("button");
    btn.style.position = "fixed";
    btn.style.bottom = "30px";
    btn.style.right = "30px";
    btn.style.zIndex = "999999";
    btn.style.padding = "15px 25px";
    btn.style.fontSize = "16px";
    btn.style.fontWeight = "bold";
    btn.style.color = "white";
    btn.style.backgroundColor = "#ff9800";
    btn.style.border = "2px solid #e68a00";
    btn.style.borderRadius = "8px";
    btn.style.cursor = "not-allowed";
    btn.style.boxShadow = "0 4px 10px rgba(0,0,0,0.5)";
    btn.innerText = "⏳ Scrolling... Do not touch";
    document.body.appendChild(btn);

    // --- 3. EXECUTE AUTO-SCROLL ---
    let scrollingElement = document.scrollingElement || document.body;
    let lastHeight = scrollingElement.scrollHeight;
    let lastChangeTime = Date.now();

    while (Date.now() - lastChangeTime < CONFIG.TIMEOUT_MS) {
        window.scrollTo(0, scrollingElement.scrollHeight);
        await new Promise(resolve => setTimeout(resolve, CONFIG.SCROLL_INTERVAL));

        let newHeight = scrollingElement.scrollHeight;

        if (newHeight > lastHeight) {
            lastHeight = newHeight;
            lastChangeTime = Date.now();
            btn.innerText = "⏳ Fetching new files...";
        } else {
            let secondsLeft = Math.round((CONFIG.TIMEOUT_MS - (Date.now() - lastChangeTime)) / 1000);
            btn.innerText = `⏳ Checking if loading... (${secondsLeft}s)`;
        }
    }

    // --- 4. ARM THE DOWNLOAD TRIGGER ---
    console.log("Scroll complete. Arming download payload.");
    btn.style.backgroundColor = "#4caf50";
    btn.style.border = "2px solid #388e3c";
    btn.style.cursor = "pointer";
    btn.innerText = "⬇️ Download JSON Tree";

    // --- 5. DOWNLOAD EVENT HANDLER ---
    btn.onclick = function () {
        // Extract the absolute base path from breadcrumbs
        let currentPath = "";
        try {
            currentPath = [...document.querySelector("div.breadcrumb").childNodes]
                .map((/** @type {HTMLElement} */ el) => el.title)
                .filter(title => title)
                .join('/');

            // Ensure the base path starts with a slash if it doesn't already
            if (currentPath && !currentPath.startsWith('/')) {
                currentPath = '/' + currentPath;
            }
        } catch (e) {
            console.warn("Breadcrumb extraction failed. Base path will be empty.");
        }

        // Run extraction logic, appending the currentPath to each title
        const dirs = [...document.querySelectorAll("a.fldr")].map((/** @type {HTMLAnchorElement} */ el) => {
            // Safely construct the full path
            let fullTarget = currentPath ? `${currentPath}/${el.title}` : el.title;
            // Clean up any accidental double slashes
            fullTarget = fullTarget.replace(/\/\//g, '/');

            return {
                href: el.href,
                title: el.title,
                absolutePath: fullTarget,
                subdirs: []
            };
        });

        // Wrap the payload
        const payload = JSON.stringify({
            basePath: currentPath,
            totalDirectories: dirs.length,
            dirs: dirs
        }, null, 2);

        // Build OS-safe filename
        let rawPath = "IDrive_Export";
        if (currentPath) {
            rawPath = currentPath.replace(/\//g, '_');
        }
        const safeFileName = rawPath.replace(/[^a-z0-9_-]/gi, '_').replace(/_+/g, '_') + ".json";

        // Trigger browser download
        const blob = new Blob([payload], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = safeFileName;
        document.body.appendChild(a);
        a.click();

        // Cleanup
        document.body.removeChild(a);
        URL.revokeObjectURL(url);

        btn.innerText = "✅ Download Complete";
        btn.style.backgroundColor = "#2196f3";
        // setTimeout(() => document.body.removeChild(btn), 3000);
    };
})();