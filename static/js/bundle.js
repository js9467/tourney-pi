// Placeholder bundle for offline use. Run 'npx esbuild src/main.js --bundle --minify --outfile=static/js/bundle.js'
// to generate a full bundle including Vue and HLS.js.
window.Vue = {
  createApp() {
    return { mount() {} };
  }
};
window.Hls = function(){};
