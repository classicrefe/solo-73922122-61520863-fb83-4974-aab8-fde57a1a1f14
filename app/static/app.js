// Toggle abnormal-item choices to the card's current inspection list.
(function () {
  const cardSelect = document.getElementById("submit-card");
  if (!cardSelect) return;
  const rows = Array.from(document.querySelectorAll("#abnormal-items .check"));

  function apply() {
    const option = cardSelect.options[cardSelect.selectedIndex];
    const raw = option ? option.dataset.items || "" : "";
    const allowed = new Set(raw ? raw.split(",") : []);
    rows.forEach((row) => {
      const box = row.querySelector(".abnormal-toggle");
      const detail = row.querySelector(".detail");
      const ok = allowed.has(row.dataset.item);
      box.disabled = !ok;
      detail.disabled = !ok;
      if (!ok) {
        box.checked = false;
        detail.value = "";
      }
    });
  }

  cardSelect.addEventListener("change", apply);
  apply();
})();
