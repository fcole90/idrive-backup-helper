async ({ fileName, cooldownMs }) => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
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

  const rows = [...document.querySelectorAll("#file_list_container > li")];
  const targetRow = rows.find((row) => {
    if (row.querySelector("a.fldr")) {
      return false;
    }

    const nameEl = row.querySelector(".file_name a") || row.querySelector(".file_name span");
    const candidateName = nameEl ? (nameEl.getAttribute("title") || nameEl.textContent.trim()) : "";
    return candidateName === fileName;
  });

  if (!targetRow) {
    return { ok: false, reason: `File row not found: ${fileName}` };
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
