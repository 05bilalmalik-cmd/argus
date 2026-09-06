(() => {
  const bind = input => {
    const container = input.closest('.file-upload') || input.parentElement;
    if (!container || container.querySelector('.file-upload__filename')) return;
    // The lab's compact fixture uses a label as the upload container. Mark it
    // with the same stable hook exposed by the real Greenhouse widget so the
    // adapter can keep a handle while the provider-owned readback changes.
    container.classList.add('file-upload');
    const filename = document.createElement('span');
    filename.className = 'file-upload__filename';
    filename.style.display = 'none';
    container.append(filename);
    input.addEventListener('change', () => {
      const file = input.files?.[0];
      filename.textContent = file?.name || '';
      filename.style.display = file ? 'inline-block' : 'none';
    });
  };

  document
    .querySelectorAll('main[data-ats="greenhouse"] input[type="file"]')
    .forEach(bind);
})();
