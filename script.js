// ==================== //
// File Upload Handling //
// ==================== //

const fileInput = document.getElementById('fileInput');
const uploadCard = document.querySelector('.upload-card');

// Drag and Drop functionality
uploadCard.addEventListener('dragover', (e) => {
    e.preventDefault();
    uploadCard.style.borderColor = 'var(--primary-color)';
    uploadCard.style.transform = 'scale(1.02)';
});

uploadCard.addEventListener('dragleave', (e) => {
    e.preventDefault();
    uploadCard.style.borderColor = 'var(--border-color)';
    uploadCard.style.transform = 'scale(1)';
});

uploadCard.addEventListener('drop', (e) => {
    e.preventDefault();
    uploadCard.style.borderColor = 'var(--border-color)';
    uploadCard.style.transform = 'scale(1)';
    
    const files = e.dataTransfer.files;
    handleFiles(files);
});

// File input change handler
fileInput.addEventListener('change', (e) => {
    const files = e.target.files;
    handleFiles(files);
});

// Handle uploaded files
function handleFiles(files) {
    if (files.length === 0) return;
    
    console.log('Files uploaded:', files.length);
    
    // Show notification
    showNotification(`${files.length}件のファイルをアップロードしました`, 'success');
    
    // Simulate processing
    Array.from(files).forEach((file, index) => {
        setTimeout(() => {
            addSlipToList(file);
        }, index * 500);
    });
}

// Add slip item to the processing list
function addSlipToList(file) {
    const slipList = document.querySelector('.slip-list');
    const badge = document.querySelector('.badge');
    
    const slipItem = document.createElement('div');
    slipItem.className = 'slip-item';
    
    const fileSize = formatFileSize(file.size);
    const fileName = file.name;
    
    slipItem.innerHTML = `
        <div class="slip-preview">
            <div class="preview-placeholder">
                <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <path d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" stroke="currentColor" stroke-width="2"/>
                </svg>
            </div>
        </div>
        <div class="slip-info">
            <h3 class="slip-name">${fileName}</h3>
            <div class="slip-meta">
                <span class="meta-item">
                    <svg class="meta-icon" viewBox="0 0 24 24" fill="none">
                        <path d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" stroke="currentColor" stroke-width="2"/>
                    </svg>
                    たった今
                </span>
                <span class="meta-item">
                    <svg class="meta-icon" viewBox="0 0 24 24" fill="none">
                        <path d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" stroke="currentColor" stroke-width="2"/>
                    </svg>
                    ${fileSize}
                </span>
            </div>
            <div class="progress-container">
                <div class="progress-bar">
                    <div class="progress-fill" style="width: 0%"></div>
                </div>
                <span class="progress-text">画像解析中... 0%</span>
            </div>
        </div>
        <div class="slip-actions">
            <button class="action-btn secondary" onclick="previewSlip(this)">
                <svg viewBox="0 0 24 24" fill="none">
                    <path d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" stroke="currentColor" stroke-width="2"/>
                    <path d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" stroke="currentColor" stroke-width="2"/>
                </svg>
            </button>
            <button class="action-btn secondary" onclick="deleteSlip(this)">
                <svg viewBox="0 0 24 24" fill="none">
                    <path d="M6 18L18 6M6 6l12 12" stroke="currentColor" stroke-width="2"/>
                </svg>
            </button>
        </div>
    `;
    
    slipList.insertBefore(slipItem, slipList.firstChild);
    
    // Update badge count
    const currentCount = parseInt(badge.textContent);
    badge.textContent = `${currentCount + 1}件`;
    
    // Animate entry
    slipItem.style.opacity = '0';
    slipItem.style.transform = 'translateY(-20px)';
    setTimeout(() => {
        slipItem.style.transition = 'all 0.3s ease-out';
        slipItem.style.opacity = '1';
        slipItem.style.transform = 'translateY(0)';
    }, 10);
    
    // Simulate processing progress
    simulateProcessing(slipItem);
}

// Simulate OCR processing
function simulateProcessing(slipItem) {
    const progressFill = slipItem.querySelector('.progress-fill');
    const progressText = slipItem.querySelector('.progress-text');
    const progressContainer = slipItem.querySelector('.progress-container');
    
    let progress = 0;
    const interval = setInterval(() => {
        progress += Math.random() * 15;
        if (progress > 100) progress = 100;
        
        progressFill.style.width = `${progress}%`;
        
        if (progress < 50) {
            progressText.textContent = `画像解析中... ${Math.floor(progress)}%`;
        } else if (progress < 100) {
            progressText.textContent = `OCR処理中... ${Math.floor(progress)}%`;
        } else {
            progressText.textContent = '処理完了';
            clearInterval(interval);
            
            // Mark as completed
            setTimeout(() => {
                completeSlipProcessing(slipItem, progressContainer);
            }, 500);
        }
    }, 300);
}

// Mark slip as completed
function completeSlipProcessing(slipItem, progressContainer) {
    slipItem.classList.add('completed');
    
    const preview = slipItem.querySelector('.preview-placeholder');
    preview.classList.add('success');
    preview.innerHTML = `
        <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" stroke="currentColor" stroke-width="2"/>
        </svg>
    `;
    
    progressContainer.innerHTML = `
        <div class="status-badge success">
            <svg viewBox="0 0 24 24" fill="none">
                <path d="M5 13l4 4L19 7" stroke="currentColor" stroke-width="2"/>
            </svg>
            処理完了
        </div>
    `;
    
    // Update actions
    const actions = slipItem.querySelector('.slip-actions');
    actions.innerHTML = `
        <button class="action-btn primary" onclick="exportPDF(this)">
            <svg viewBox="0 0 24 24" fill="none">
                <path d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" stroke="currentColor" stroke-width="2"/>
            </svg>
            PDF出力
        </button>
        <button class="action-btn secondary" onclick="editSlip(this)">
            <svg viewBox="0 0 24 24" fill="none">
                <path d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" stroke="currentColor" stroke-width="2"/>
            </svg>
        </button>
    `;
    
    showNotification('伝票の処理が完了しました', 'success');
}

// ==================== //
// Action Handlers      //
// ==================== //

function previewSlip(button) {
    const slipItem = button.closest('.slip-item');
    const fileName = slipItem.querySelector('.slip-name').textContent;
    showNotification(`${fileName} をプレビュー表示`, 'info');
}

function deleteSlip(button) {
    const slipItem = button.closest('.slip-item');
    const fileName = slipItem.querySelector('.slip-name').textContent;
    const badge = document.querySelector('.badge');
    
    if (confirm(`${fileName} を削除しますか?`)) {
        slipItem.style.transition = 'all 0.3s ease-out';
        slipItem.style.opacity = '0';
        slipItem.style.transform = 'translateX(-20px)';
        
        setTimeout(() => {
            slipItem.remove();
            const currentCount = parseInt(badge.textContent);
            badge.textContent = `${currentCount - 1}件`;
            showNotification('伝票を削除しました', 'info');
        }, 300);
    }
}

function exportPDF(button) {
    const slipItem = button.closest('.slip-item');
    const fileName = slipItem.querySelector('.slip-name').textContent;
    
    // Simulate PDF export
    button.disabled = true;
    button.innerHTML = `
        <svg viewBox="0 0 24 24" fill="none" class="spinning">
            <path d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" stroke="currentColor" stroke-width="2"/>
        </svg>
        出力中...
    `;
    
    setTimeout(() => {
        button.disabled = false;
        button.innerHTML = `
            <svg viewBox="0 0 24 24" fill="none">
                <path d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" stroke="currentColor" stroke-width="2"/>
            </svg>
            PDF出力
        `;
        showNotification(`${fileName} をPDFで出力しました`, 'success');
    }, 2000);
}

function editSlip(button) {
    const slipItem = button.closest('.slip-item');
    const fileName = slipItem.querySelector('.slip-name').textContent;
    showNotification(`${fileName} を編集モードで開きます`, 'info');
}

// ==================== //
// Utility Functions    //
// ==================== //

function formatFileSize(bytes) {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
}

function showNotification(message, type = 'info') {
    // Create notification element
    const notification = document.createElement('div');
    notification.className = `notification notification-${type}`;
    notification.textContent = message;
    
    // Add styles
    notification.style.cssText = `
        position: fixed;
        top: 20px;
        right: 20px;
        padding: 1rem 1.5rem;
        background: var(--bg-card);
        backdrop-filter: blur(20px);
        border: 1px solid var(--border-color);
        border-radius: var(--radius-md);
        color: var(--text-primary);
        font-weight: 500;
        box-shadow: var(--shadow-xl);
        z-index: 1000;
        animation: slideIn 0.3s ease-out;
        max-width: 400px;
    `;
    
    if (type === 'success') {
        notification.style.borderLeft = '4px solid var(--success-color)';
    } else if (type === 'error') {
        notification.style.borderLeft = '4px solid var(--danger-color)';
    } else {
        notification.style.borderLeft = '4px solid var(--primary-color)';
    }
    
    document.body.appendChild(notification);
    
    // Remove after 3 seconds
    setTimeout(() => {
        notification.style.animation = 'slideOut 0.3s ease-out';
        setTimeout(() => {
            notification.remove();
        }, 300);
    }, 3000);
}

// Add animation styles
const style = document.createElement('style');
style.textContent = `
    @keyframes slideIn {
        from {
            transform: translateX(400px);
            opacity: 0;
        }
        to {
            transform: translateX(0);
            opacity: 1;
        }
    }
    
    @keyframes slideOut {
        from {
            transform: translateX(0);
            opacity: 1;
        }
        to {
            transform: translateX(400px);
            opacity: 0;
        }
    }
    
    .spinning {
        animation: spin 1s linear infinite;
    }
    
    @keyframes spin {
        from {
            transform: rotate(0deg);
        }
        to {
            transform: rotate(360deg);
        }
    }
`;
document.head.appendChild(style);

// ==================== //
// Initialize           //
// ==================== //

console.log('伝票データ化システム - モックアップ起動');
showNotification('システムが起動しました', 'success');
