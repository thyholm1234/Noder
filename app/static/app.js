// Noder - Client-side JavaScript

document.addEventListener('DOMContentLoaded', function () {

    // Auto-hide flash messages after 4 seconds
    const flash = document.querySelector('.flash-message');
    if (flash) {
        setTimeout(function () {
            flash.style.transition = 'opacity 0.4s';
            flash.style.opacity = '0';
            setTimeout(function () { flash.remove(); }, 400);
        }, 4000);
    }

    // Toggle part_name visibility based on file_type selection
    const fileTypeSelect = document.getElementById('file_type_select');
    const partNameGroup = document.getElementById('part_name_group');
    if (fileTypeSelect && partNameGroup) {
        function updatePartNameVisibility() {
            partNameGroup.style.display = fileTypeSelect.value === 'audio' ? '' : 'none';
        }
        updatePartNameVisibility();
        fileTypeSelect.addEventListener('change', updatePartNameVisibility);
    }

    // Highlight active nav link
    const currentPath = window.location.pathname;
    document.querySelectorAll('.nav-link').forEach(function (link) {
        if (link.getAttribute('href') === currentPath) {
            link.classList.add('active');
        }
    });

});
