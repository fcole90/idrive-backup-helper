async ({ title, titleCandidates, settleMinMs, settleMaxMs }) => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const randomDelay = () => settleMinMs + Math.floor(Math.random() * (settleMaxMs - settleMinMs + 1));
  const candidateNames = Array.isArray(titleCandidates) && titleCandidates.length > 0
    ? titleCandidates
    : [title];

  const forceClick = (el) => {
    ["mouseenter", "mouseover", "mousedown", "mouseup", "click"].forEach((eventName) => {
      el.dispatchEvent(new MouseEvent(eventName, {
        view: window,
        bubbles: true,
        cancelable: true,
        buttons: 1,
      }));
    });
  };

  const breadcrumb = document.querySelector("div.breadcrumb");
  if (!breadcrumb) {
    return { ok: false, reason: "Breadcrumb container not found" };
  }

  const nodes = [...breadcrumb.childNodes].filter((node) => node.nodeType === 1);
  const target = nodes.find((el) => candidateNames.includes(el.title));
  if (!target) {
    return { ok: false, reason: `Breadcrumb not found: ${candidateNames.join(" or ")}` };
  }

  target.scrollIntoView({ block: "center" });
  await sleep(randomDelay());
  forceClick(target);
  await sleep(randomDelay());

  return { ok: true };
};
