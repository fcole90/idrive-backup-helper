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

  const isErrorBoxVisible = () => {
    const box = document.querySelector(".error_msg");
    if (!box) {
      return false;
    }
    const style = window.getComputedStyle(box);
    return (
      style.display !== "none" &&
      style.visibility !== "hidden" &&
      box.getClientRects().length > 0
    );
  };

  folderTrigger.scrollIntoView({ block: "center" });
  await sleep(randomDelay());
  forceClick(folderTrigger);

  // After the click IDrive either navigates into the folder or, when the backend
  // refuses to open it, flashes a global error box ("There is some problem. Try
  // later.") and stays on the current folder. Poll for that box across the normal
  // post-click settle delay: a broken folder is reported immediately instead of
  // silently "succeeding" and pushing the caller into a long retry loop, while a
  // healthy click never shows the box and pays the same total delay as before.
  const pollSliceMs = 150;
  const errorDeadline = Date.now() + randomDelay();
  while (Date.now() < errorDeadline) {
    if (isErrorBoxVisible()) {
      const message = (document.querySelector(".error_msg").textContent || "").trim();
      return {
        ok: false,
        folderUnavailable: true,
        reason: `IDrive refused to open folder "${candidateNames[0]}": ${message || "There is some problem. Try later."}`,
      };
    }
    await sleep(pollSliceMs);
  }

  return { ok: true };
};
