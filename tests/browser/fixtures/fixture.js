"use strict";
const form = document.querySelector("form");
const review = document.getElementById("review");
if (review) {
  for (const [key, value] of Object.entries(JSON.parse(sessionStorage.getItem("fixture_answers") || "{}"))) {
    const title = document.createElement("dt"), description = document.createElement("dd"); title.textContent = key; description.textContent = value; review.append(title, description);
  }
}
if (form) form.addEventListener("submit", event => {
  event.preventDefault();
  if (!form.checkValidity()) {form.reportValidity(); return;}
  const values = Object.fromEntries([...new FormData(form)].map(([key, value]) => [key, value instanceof File ? value.name : value]));
  sessionStorage.setItem("fixture_answers", JSON.stringify({...JSON.parse(sessionStorage.getItem("fixture_answers") || "{}"), ...values}));
  location.href = form.getAttribute("action");
});
