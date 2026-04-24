document.addEventListener("click", (event) => {
  const trigger = event.target.closest("[data-add-item-row]");
  if (!trigger) {
    return;
  }

  const tbody = document.querySelector("#items-table tbody");
  if (!tbody) {
    return;
  }

  const row = document.createElement("tr");
  row.innerHTML = `
    <td>
      <input type="hidden" name="item_id" value="" />
      <input name="item_description" value="" />
    </td>
    <td><input type="number" step="0.001" name="item_quantity" value="" /></td>
    <td><input name="item_unit" value="" /></td>
    <td><input type="number" step="0.01" name="item_unit_price" value="" /></td>
    <td><input type="number" step="0.01" name="item_amount" value="" /></td>
    <td><input type="number" step="0.01" name="item_tax_rate" value="" /></td>
  `;
  tbody.appendChild(row);
});
