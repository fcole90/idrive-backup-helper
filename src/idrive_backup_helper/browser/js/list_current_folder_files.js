async ({ scrollIntervalMs, maxIdleTicks }) => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  async function scrollCurrentViewToBottom() {
    const scrollingEl = document.scrollingElement || document.body;
    let lastHeight = scrollingEl.scrollHeight;
    let idleTicks = 0;

    while (idleTicks < maxIdleTicks) {
      window.scrollBy(0, 800);
      await sleep(scrollIntervalMs);

      if (scrollingEl.scrollHeight > lastHeight) {
        lastHeight = scrollingEl.scrollHeight;
        idleTicks = 0;
      } else if (
        Math.ceil(scrollingEl.scrollTop + window.innerHeight) >=
        scrollingEl.scrollHeight - 10
      ) {
        idleTicks += 1;
      }
    }
  }

  await scrollCurrentViewToBottom();

  return [...document.querySelectorAll("#file_list_container > li")]
    .map((row, rowIndex) => {
      const folderAnchor = row.querySelector("a.fldr");
      if (folderAnchor) {
        const folderName = folderAnchor.getAttribute("title") || folderAnchor.textContent.trim();
        if (!folderName) {
          return null;
        }

        return {
          entryType: "folder",
          folderName,
          href: folderAnchor.href,
        };
      }

      const nameEl = row.querySelector(".file_name a") || row.querySelector(".file_name span");
      if (!nameEl) {
        return null;
      }

      const fileName = nameEl.getAttribute("title") || nameEl.textContent.trim();
      const sizeEl = row.querySelector(".file_size");
      const dateEl = row.querySelector(".file_date");

      return {
        entryType: "file",
        fileName,
        rowIndex,
        serverSizeText: sizeEl ? sizeEl.textContent.trim() : null,
        serverModifiedText: dateEl ? dateEl.textContent.trim() : null,
      };
    })
    .filter(Boolean);
};
