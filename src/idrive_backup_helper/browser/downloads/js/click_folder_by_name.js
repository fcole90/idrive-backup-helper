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

  // IDrive shows the folder-open failure ("There is some problem. Try later.") in
  // one of several `.error_msg` divs on the page — the shown one is NOT necessarily
  // the first in the DOM, so scan them all and match the banner text rather than
  // trusting a single querySelector (which kept landing on a permanently-hidden
  // box). Return the visible banner's text, or null when none is showing.
  const visibleErrorBannerText = () => {
    const boxes = [...document.querySelectorAll(".error_msg")];
    for (const box of boxes) {
      const style = window.getComputedStyle(box);
      const visible =
        style.display !== "none" &&
        style.visibility !== "hidden" &&
        box.getClientRects().length > 0;
      if (!visible) {
        continue;
      }
      const text = (box.textContent || "").trim();
      if (/problem|try later/i.test(text)) {
        return text;
      }
    }
    return null;
  };

  folderTrigger.scrollIntoView({ block: "center" });
  await sleep(randomDelay());
  forceClick(folderTrigger);

  // After the click IDrive either navigates into the folder or, when the backend
  // refuses to open it, flashes the error banner and stays on the current folder.
  // Poll for that banner across the normal post-click settle delay: a broken folder
  // is reported immediately instead of silently "succeeding" and pushing the caller
  // into a long retry loop, while a healthy click never shows it and pays the same
  // total delay as before.
  const pollSliceMs = 150;
  const errorDeadline = Date.now() + randomDelay();
  while (Date.now() < errorDeadline) {
    const bannerText = visibleErrorBannerText();
    if (bannerText) {
      return {
        ok: false,
        folderUnavailable: true,
        reason: `IDrive refused to open folder "${candidateNames[0]}": ${bannerText}`,
      };
    }
    await sleep(pollSliceMs);
  }

  return { ok: true };
};
