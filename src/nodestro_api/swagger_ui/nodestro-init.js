// Starts Swagger UI on the API reference page (Nodestro). A file of its own, so the page needs no inline script
// under its Content-Security-Policy; the OpenAPI document's URL comes from the page (data-openapi-url).
window.addEventListener("load", function () {
  var root = document.getElementById("swagger-ui");
  window.ui = SwaggerUIBundle({
    url: root.getAttribute("data-openapi-url"),
    dom_id: "#swagger-ui",
    layout: "BaseLayout",
    deepLinking: true,
    showExtensions: true,
    showCommonExtensions: true,
    // No request to the public validator service (validator.swagger.io).
    validatorUrl: null,
    presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
  });
});
