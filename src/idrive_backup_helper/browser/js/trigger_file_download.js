async ({ fileName, rowIndex, cooldownMs }) => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
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

  const findTargetRow = () => {
    const rows = [...document.querySelectorAll("#file_list_container > li")];
    return rows.find((row) => {
      if (row.querySelector("a.fldr")) {
        return false;
      }

      const nameEl = row.querySelector(".file_name a") || row.querySelector(".file_name span");
      const candidateName = nameEl ? (nameEl.getAttribute("title") || nameEl.textContent.trim()) : "";
      return candidateName === fileName;
    });
  };

  let targetRow = findTargetRow();
  if (!targetRow && Number.isInteger(rowIndex)) {
    window.scrollTo(0, Math.max(0, rowIndex * 48 - Math.floor(window.innerHeight / 2)));
    await sleep(600);
    targetRow = findTargetRow();
  }

  let scrollAttempts = 0;
  while (!targetRow && scrollAttempts < 40) {
    window.scrollBy(0, 900);
    await sleep(250);
    targetRow = findTargetRow();
    scrollAttempts += 1;

    if (Math.ceil(scrollingEl.scrollTop + window.innerHeight) >= scrollingEl.scrollHeight - 10) {
      break;
    }
  }

  if (!targetRow) {
    return { ok: false, reason: `File row not found after scrolling: ${fileName}` };
  }

  targetRow.scrollIntoView({ block: "center" });
  await sleep(400);

  const menuTrigger =
    targetRow.querySelector(".file_menu_list a img") ||
    targetRow.querySelector(".file_menu_list a");
  if (!menuTrigger) {
    return { ok: false, reason: `File menu not found: ${fileName}` };
  }

  forceClick(menuTrigger);
  await sleep(400);

  const downloadButton = document.querySelector("ul#content.menu_inside .file_menu_download");
  if (!downloadButton || window.getComputedStyle(downloadButton).display === "none") {
    forceClick(document.body);
    return { ok: false, reason: `Download action unavailable: ${fileName}` };
  }

  const parentLi = downloadButton.closest("li");
  if (parentLi) {
    parentLi.classList.add("hover");
  }
  downloadButton.classList.add("hover");
  forceClick(downloadButton);
  await sleep(cooldownMs);
  forceClick(document.body);

  return { ok: true };
};
