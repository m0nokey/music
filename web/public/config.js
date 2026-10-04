/* Runtime-relative URLs keep the static bundle portable under any reverse-proxy prefix. */
(() => {
  const scriptUrl = new URL(document.currentScript.src, window.location.href);
  const basePath = scriptUrl.pathname.replace(/\/config\.js$/, "").replace(/\/$/, "");
  window.MUSIC_CONFIG = {
    basePath,
    apiBasePath: `${basePath}/api`,
    hlsBasePath: `${basePath}/live`,
  };
})();
