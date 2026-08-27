(function () {
  function qs(id) {
    return document.getElementById(id);
  }

  async function readError(res) {
    try {
      const data = await res.json();
      if (typeof data.detail === "string") return data.detail;
      if (Array.isArray(data.detail) && data.detail[0] && data.detail[0].msg) {
        return data.detail[0].msg;
      }
    } catch (_err) {
      /* ignore */
    }
    return "Something went wrong. Please try again.";
  }

  function formatPrice(amount, currency) {
    const symbol = currency === "INR" ? "₹" : `${currency} `;
    return `${symbol}${amount}`;
  }

  async function loadPackages(listEl) {
    listEl.innerHTML = '<p class="affeeso-hint">Loading packages…</p>';
    const res = await fetch("/coin-packages");
    if (res.status === 401) {
      window.location.href = "/?error=Please login first.";
      return;
    }
    if (!res.ok) {
      listEl.innerHTML = `<p class="upi-pay-error">${await readError(res)}</p>`;
      return;
    }
    const packages = await res.json();
    const active = packages.filter((item) => item.is_active !== false);
    if (!active.length) {
      listEl.innerHTML = '<p class="affeeso-hint">No coin packages are available right now.</p>';
      return;
    }
    listEl.innerHTML = "";
    active.forEach((pkg) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "upi-package-btn";
      button.dataset.packageId = String(pkg.id);
      button.innerHTML = `<span>+${pkg.coins} Coins</span><span>${formatPrice(pkg.price, pkg.currency)}</span>`;
      button.addEventListener("click", () => createPayment(pkg.id));
      listEl.appendChild(button);
    });
  }

  function openModal() {
    const overlay = qs("upiPaymentModal");
    if (!overlay) return;
    overlay.classList.add("is-open");
  }

  function closeModal() {
    const overlay = qs("upiPaymentModal");
    if (!overlay) return;
    overlay.classList.remove("is-open");
  }

  function showPayView() {
    qs("upiPayView").hidden = false;
    qs("upiPayDone").hidden = true;
    qs("upiPayError").textContent = "";
    qs("upiPaySubmit").disabled = false;
  }

  async function createPayment(packageId) {
    showPayView();
    qs("upiPayError").textContent = "Creating payment…";
    openModal();
    const res = await fetch("/payments/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ package_id: packageId }),
    });
    if (!res.ok) {
      qs("upiPayError").textContent = await readError(res);
      qs("upiPaySubmit").disabled = true;
      return;
    }
    const payment = await res.json();
    qs("upiPayError").textContent = "";
    qs("upiPayCoins").textContent = `+${payment.coins} Coins`;
    qs("upiPayAmount").textContent = formatPrice(payment.amount, payment.currency);
    qs("upiPayUpiId").textContent = payment.upi_id || "-";
    qs("upiPayId").textContent = payment.payment_id;
    qs("upiPayQr").src = payment.qr_data_url || "";
    qs("upiPaySubmit").dataset.paymentId = payment.payment_id;
    qs("upiPaySubmit").disabled = false;
  }

  async function submitPayment() {
    const button = qs("upiPaySubmit");
    const paymentId = button.dataset.paymentId;
    if (!paymentId) return;
    button.disabled = true;
    const res = await fetch(`/payments/${encodeURIComponent(paymentId)}/submit`, {
      method: "POST",
    });
    if (!res.ok) {
      qs("upiPayError").textContent = await readError(res);
      button.disabled = false;
      return;
    }
    qs("upiPayView").hidden = true;
    qs("upiPayDone").hidden = false;
  }

  function bindDropdowns() {
    document.querySelectorAll(".js-coin-packages-dropdown").forEach((details) => {
      const listEl = details.querySelector(".js-coin-packages-list");
      if (!listEl) return;
      details.addEventListener("toggle", () => {
        if (details.open) {
          loadPackages(listEl).catch(() => {
            listEl.innerHTML = '<p class="upi-pay-error">Could not load packages.</p>';
          });
        }
      });
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindDropdowns();
    const closeBtn = qs("upiPayClose");
    const doneClose = qs("upiPayDoneClose");
    const submitBtn = qs("upiPaySubmit");
    if (closeBtn) closeBtn.addEventListener("click", closeModal);
    if (doneClose) doneClose.addEventListener("click", closeModal);
    if (submitBtn) submitBtn.addEventListener("click", submitPayment);
    const overlay = qs("upiPaymentModal");
    if (overlay) {
      overlay.addEventListener("click", (event) => {
        if (event.target === overlay) closeModal();
      });
    }
  });
})();
