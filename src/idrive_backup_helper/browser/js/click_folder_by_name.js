async ({ folderName, folderNameCandidates, settleMinMs, settleMaxMs }) => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const randomDelay = () => settleMinMs + Math.floor(Math.random() * (settleMaxMs - settleMinMs + 1));
  const candidateNames = Array.isArray(folderNameCandidates) && folderNameCandidates.length > 0
    ? folderNameCandidates
    : [folderName];
  const scrollingEl = document.scrollingElement || document.body;
  const forceClick = (el) => {
    if (!el) {
      return;
    }

    ["mouseenter", "mouseover", "mousedown", "mouseup", "click"].forEach((eventName) => {
      el.dispatchEvent(new MouseEvent(eventName, {
        view: window,
        bubbles: true,
        cancelable: true,
        buttons: 1,
      }));
    });
  };

  const findFolderTrigger = () => {
    const rows = [...document.querySelectorAll("#file_list_container > li")];
    const targetRow = rows.find((row) => {
      const nameEl = row.querySelector("a.fldr") || row.querySelector(".file_name a") || row.querySelector(".file_name span");
      const candidateName = nameEl ? (nameEl.getAttribute("title") || nameEl.textContent.trim()) : "";
      return candidateNames.includes(candidateName);
    });

    if (!targetRow) {
      return null;
    }

    return targetRow.querySelector("a.fldr") || targetRow.querySelector(".file_name a") || targetRow;
  };

  let folderTrigger = findFolderTrigger();
  let scrollAttempts = 0;
  while (!folderTrigger && scrollAttempts < 80) {
    window.scrollBy(0, 700 + Math.floor(Math.random() * 500));
    await sleep(randomDelay());
    folderTrigger = findFolderTrigger();
    scrollAttempts += 1;

    if (Math.ceil(scrollingEl.scrollTop + window.innerHeight) >= scrollingEl.scrollHeight - 10) {
      break;
    }
  }

  if (!folderTrigger) {
    return { ok: false, reason: `Folder row not found after scrolling: ${candidateNames.join(" or ")}` };
  }

  folderTrigger.scrollIntoView({ block: "center" });
  await sleep(randomDelay());
  forceClick(folderTrigger);
  await sleep(randomDelay());

  return { ok: true };
};
