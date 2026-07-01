async ({ addressIndex, settleMinMs, settleMaxMs }) => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const randomDelay = () => settleMinMs + Math.floor(Math.random() * (settleMaxMs - settleMinMs + 1));

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

  // A crumb's addressindex is its position in the folder path, so we click by that
  // rather than by title: on a deep path the breadcrumb collapses leading crumbs
  // and truncates their visible text, but the surviving crumbs keep addressindex.
  const target = [...breadcrumb.querySelectorAll("a.addfldr")].find(
    (el) => Number.parseInt(el.getAttribute("addressindex"), 10) === addressIndex
  );
  if (!target) {
    return { ok: false, reason: `Breadcrumb addressindex not found: ${addressIndex}` };
  }

  target.scrollIntoView({ block: "center" });
  await sleep(randomDelay());
  forceClick(target);
  await sleep(randomDelay());

  return { ok: true };
};
