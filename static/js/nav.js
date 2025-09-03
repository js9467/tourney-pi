document.addEventListener('DOMContentLoaded', () => {
  const placeholder = document.getElementById('nav-placeholder');
  if (!placeholder) return;
  fetch('/static/components/nav.html')
    .then(res => res.text())
    .then(html => {
      placeholder.innerHTML = html;
    })
    .catch(err => console.error('Failed to load navigation', err));
});
