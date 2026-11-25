async function collectDomesticPayment(invoice) {
  const response = await fetch(
    `/api/supplier-terms?company=${encodeURIComponent(invoice.company || "")}` +
    `&supplier=${encodeURIComponent(invoice.supplier || "")}`
  );
  const terms = response.ok ? await response.json() : {};
  const dialog = document.getElementById("payment-dialog");
  const form = document.getElementById("payment-form");
  const method = document.getElementById("payment-method");
  const account = document.getElementById("payment-supplier-account");
  const profiles = terms.profiles || [];
  account.innerHTML = profiles.length
    ? profiles.map(profile =>
        `<option value="${escapeAttr(profile.supplier_account_number || "")}">` +
        `${escapeHtml(profile.supplier_account_number || "No account number")} — ` +
        `${escapeHtml(profile.bank_account || "No bank specified")}` +
        `</option>`
      ).join("")
    : '<option value="">No configured supplier account</option>';
  account.disabled = !profiles.length;
  const applyProfile = () => {
    const profile = profiles.find(item => item.supplier_account_number === account.value) || profiles[0] || {};
    method.value = invoice.payment_method || "";
    document.getElementById("payment-terms").value = profile.payment_terms_notice || "";
    document.getElementById("payment-bank-account").value = profile.bank_account || "";
  };
  account.onchange = applyProfile;
  applyProfile();
  document.getElementById("payment-date").value = localTodayIsoDate();
  document.getElementById("payment-reference").value = "";
  document.getElementById("payment-supplier-context").textContent = [
    invoiceAccessibleName(invoice) || invoice.supplier || "Supplier",
    invoice.invoice_value == null || invoice.invoice_value === ""
      ? ""
      : formatMoney(invoice.invoice_value, invoice.currency),
  ].filter(Boolean).join(" · ");

  return new Promise(resolve => {
    let settled = false;
    const finish = value => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    form.onsubmit = event => {
      event.preventDefault();
      const payment = {
        payment_date: document.getElementById("payment-date").value,
        supplier_account_number: account.value || null,
        payment_reference: document.getElementById("payment-reference").value,
        payment_method: method.value,
      };
      dialog.close();
      finish(payment);
    };
    document.getElementById("payment-cancel").onclick = () => dialog.close();
    dialog.onclose = () => finish(null);
    dialog.showModal();
  });
}

// The IRJ mode per company (automatic or manual from paper
// records). Loaded here, not only in the admin panel, so Purchase
// Ledger's Sage registration knows a company's mode too.
async function loadIrjConfigurations() {
  const response = await fetch("/api/irj-configurations");
  if (!response.ok) return;
  const settings = await response.json();
  irjConfigurations = new Map(
    settings.map(setting => [setting.company, setting])
  );
}

async function collectSageRegistration(invoice) {
  await loadIrjConfigurations();
  const dialog = document.getElementById("sage-registration-dialog");
  const form = document.getElementById("sage-registration-form");
  const irjInput = document.getElementById("sage-registration-irj");
  const errorEl = document.getElementById("sage-registration-error");
  document.getElementById("sage-registration-context").textContent =
    `${invoice.supplier || "Supplier"} — ${invoice.original_filename}`;
  irjInput.value = invoice.irj_number || "";
  const irjSetting = irjConfigurations.get(invoice.company);
  const isManual = irjSetting ? irjSetting.mode === "manual" : false;
  irjInput.readOnly = !isManual && Boolean(invoice.irj_number);
  irjInput.title = isManual
    ? "Enter the IRJ from the paper records."
    : "This IRJ was allocated automatically.";
  errorEl.textContent = "";

  return new Promise(resolve => {
    let settled = false;
    const finish = value => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    form.onsubmit = event => {
      event.preventDefault();
      const irjNumber = irjInput.value.trim();
      if (!/^\d{6}$/.test(irjNumber)) {
        errorEl.textContent = "Enter exactly six digits.";
        return;
      }
      dialog.close();
      finish(irjNumber);
    };
    document.getElementById("sage-registration-cancel").onclick = () => dialog.close();
    dialog.onclose = () => finish(null);
    dialog.showModal();
    irjInput.focus();
  });
}

// ---------------------------------------------------------
// In-page dialogs replacing window.prompt / window.confirm.
// formDialog() resolves to an object of field values, or null
// when cancelled (Cancel button, Escape, or closing the dialog).
// ---------------------------------------------------------
const uiDialog = document.getElementById("ui-dialog");
let uiDialogPending = null;

function formDialog({
  title,
  message = "",
  fields = [],
  confirmLabel = "OK",
  cancelLabel = "Cancel",
  tone = "default",
  scrollMessage = false,
  context = "",
}) {
  if (uiDialogPending) uiDialogPending(null);
  const form = document.getElementById("ui-dialog-form");
  const fieldContainer = document.getElementById("ui-dialog-fields");
  const messageEl = document.getElementById("ui-dialog-message");
  const errorEl = document.getElementById("ui-dialog-error");
  const confirmButton = document.getElementById("ui-dialog-confirm");
  const cancelButton = document.getElementById("ui-dialog-cancel");
  const opener = document.activeElement;
  document.getElementById("ui-dialog-title").textContent = title;
  const contextEl = document.getElementById("ui-dialog-context");
  contextEl.textContent = context;
  contextEl.hidden = !context;
  messageEl.textContent = message;
  messageEl.hidden = !message;
  messageEl.classList.toggle("scroll", scrollMessage);
  errorEl.textContent = "";
  confirmButton.textContent = confirmLabel;
  cancelButton.textContent = cancelLabel;
  confirmButton.className = tone === "danger" ? "danger-solid" : "primary";
  form.classList.toggle("dialog-tone-danger", tone === "danger");
  fieldContainer.replaceChildren();
  fieldContainer.hidden = !fields.length;
  const controls = [];
  fields.forEach((field, index) => {
    const label = document.createElement("label");
    const caption = document.createElement("span");
    caption.textContent = field.label;
    if (field.required) {
      const mark = document.createElement("span");
      mark.className = "required-mark";
      mark.setAttribute("aria-hidden", "true");
      mark.textContent = " *";
      caption.appendChild(mark);
    }
    label.appendChild(caption);
    const control = document.createElement(
      field.multiline ? "textarea" : "input"
    );
    if (!field.multiline) control.type = field.type || "text";
    control.id = `ui-dialog-field-${index}`;
    control.value = field.value ?? "";
    control.required = Boolean(field.required);
    control.placeholder = field.placeholder || "";
    control.autocomplete = "off";
    if (field.required) control.setAttribute("aria-required", "true");
    label.appendChild(control);
    fieldContainer.appendChild(label);
    controls.push([field.name, control]);
  });

  return new Promise(resolve => {
    const finish = value => {
      if (uiDialogPending !== finish) return;
      uiDialogPending = null;
      form.onsubmit = null;
      cancelButton.onclick = null;
      uiDialog.onclose = null;
      if (uiDialog.open) uiDialog.close();
      // Return focus to whatever opened the dialog when it is
      // still on the page; otherwise leave focus where it lands.
      if (opener && opener.isConnected && typeof opener.focus === "function") {
        window.requestAnimationFrame(() => {
          if (!opener.disabled) opener.focus();
        });
      }
      resolve(value);
    };
    uiDialogPending = finish;
    form.onsubmit = event => {
      event.preventDefault();
      for (const [, control] of controls) {
        if (control.required && !control.value) {
          errorEl.textContent = "Please complete the required field.";
          control.focus();
          return;
        }
      }
      const values = {};
      for (const [name, control] of controls) values[name] = control.value;
      finish(values);
    };
    cancelButton.onclick = () => finish(null);
    uiDialog.onclose = () => finish(null);
    uiDialog.showModal();
    const firstControl = controls.length ? controls[0][1] : null;
    if (firstControl) {
      firstControl.focus();
    } else if (tone === "danger") {
      cancelButton.focus();
    } else {
      confirmButton.focus();
    }
  });
}

// Resolves true when confirmed, false when cancelled.
async function confirmDialog(options) {
  return (await formDialog({ ...options, fields: [] })) !== null;
}

// Resolves to the entered text, or null when cancelled. A required
// prompt cannot be submitted empty.
async function promptDialog({
  title,
  message = "",
  label,
  value = "",
  required = false,
  multiline = true,
  type = "text",
  placeholder = "",
  confirmLabel = "OK",
  tone = "default",
  context = "",
}) {
  const result = await formDialog({
    title,
    message,
    confirmLabel,
    tone,
    context,
    fields: [{ name: "value", label, value, required, multiline, type, placeholder }],
  });
  return result === null ? null : result.value;
}
