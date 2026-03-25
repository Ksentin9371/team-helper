/**
 * 统一模态窗口组件
 * 提供 alert、confirm、prompt 的美化版本
 */

const Modal = {
    // 初始化模态窗口HTML（只需调用一次）
    init() {
        if (document.getElementById('custom-modal')) return; // 已初始化
        
        const modalHTML = `
            <div id="custom-modal" class="fixed inset-0 z-50 hidden items-center justify-center p-0 sm:p-4" style="background-color: rgba(0, 0, 0, 0.5);">
                <div class="bg-white w-full h-full sm:h-auto sm:rounded-lg shadow-xl max-w-md animate-modal flex flex-col">
                    <div id="modal-header" class="px-6 py-4 border-b border-gray-200 flex-shrink-0">
                        <h3 id="modal-title" class="text-lg font-semibold text-gray-900"></h3>
                    </div>
                    <div id="modal-body" class="px-6 py-4 flex-grow overflow-y-auto">
                        <p id="modal-message" class="text-sm text-gray-700 whitespace-pre-wrap"></p>
                        <input id="modal-input" type="text" class="hidden mt-3 w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500" />
                    </div>
                    <div id="modal-footer" class="px-6 py-4 border-t border-gray-200 flex justify-end gap-3 flex-shrink-0 mb-safe">
                        <button id="modal-cancel" class="hidden px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 rounded-md hover:bg-gray-200 transition-colors">
                            取消
                        </button>
                        <button id="modal-confirm" class="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-md hover:bg-blue-700 transition-colors">
                            确定
                        </button>
                    </div>
                </div>
            </div>
            <style>
                @supports (padding-bottom: env(safe-area-inset-bottom)) {
                    .mb-safe {
                        margin-bottom: env(safe-area-inset-bottom);
                    }
                }
                @keyframes modalFadeIn {
                    from {
                        opacity: 0;
                        transform: scale(0.95) translateY(-10px);
                    }
                    to {
                        opacity: 1;
                        transform: scale(1) translateY(0);
                    }
                }
                .animate-modal {
                    animation: modalFadeIn 0.2s ease-out;
                }
            </style>
        `;
        
        document.body.insertAdjacentHTML('beforeend', modalHTML);
    },
    
    // 显示模态窗口
    show(title, message, type = 'alert', defaultValue = '') {
        return new Promise((resolve) => {
            this.init();
            
            const modal = document.getElementById('custom-modal');
            const modalTitle = document.getElementById('modal-title');
            const modalMessage = document.getElementById('modal-message');
            const modalInput = document.getElementById('modal-input');
            const modalCancel = document.getElementById('modal-cancel');
            const modalConfirm = document.getElementById('modal-confirm');
            
            // 设置标题和内容
            modalTitle.textContent = title;
            modalMessage.textContent = message;
            
            // 根据类型配置按钮和输入框
            if (type === 'prompt') {
                modalInput.classList.remove('hidden');
                modalInput.value = defaultValue;
                modalCancel.classList.remove('hidden');
                modalConfirm.textContent = '确定';
                // 聚焦输入框
                setTimeout(() => modalInput.focus(), 100);
            } else if (type === 'confirm') {
                modalInput.classList.add('hidden');
                modalCancel.classList.remove('hidden');
                modalConfirm.textContent = '确定';
            } else {
                modalInput.classList.add('hidden');
                modalCancel.classList.add('hidden');
                modalConfirm.textContent = '确定';
            }
            
            // 显示模态窗口
            modal.classList.remove('hidden');
            modal.classList.add('flex');
            
            // 确定按钮事件
            const confirmHandler = () => {
                cleanup();
                if (type === 'prompt') {
                    resolve(modalInput.value);
                } else if (type === 'confirm') {
                    resolve(true);
                } else {
                    resolve(true);
                }
            };
            
            // 取消按钮事件
            const cancelHandler = () => {
                cleanup();
                if (type === 'prompt') {
                    resolve(null);
                } else {
                    resolve(false);
                }
            };
            
            // 清理事件监听器
            const cleanup = () => {
                modal.classList.add('hidden');
                modal.classList.remove('flex');
                modalConfirm.removeEventListener('click', confirmHandler);
                modalCancel.removeEventListener('click', cancelHandler);
                modal.removeEventListener('click', backgroundHandler);
                document.removeEventListener('keydown', keyHandler);
            };
            
            // 点击背景关闭
            const backgroundHandler = (e) => {
                if (e.target === modal) {
                    cancelHandler();
                }
            };
            
            // ESC 键关闭
            const keyHandler = (e) => {
                if (e.key === 'Escape') {
                    cancelHandler();
                } else if (e.key === 'Enter' && type !== 'prompt') {
                    confirmHandler();
                }
            };
            
            modalConfirm.addEventListener('click', confirmHandler);
            modalCancel.addEventListener('click', cancelHandler);
            modal.addEventListener('click', backgroundHandler);
            document.addEventListener('keydown', keyHandler);
        });
    },
    
    // Alert 弹窗
    alert(message, title = '提示') {
        return this.show(title, message, 'alert');
    },
    
    // Confirm 弹窗
    confirm(message, title = '确认') {
        return this.show(title, message, 'confirm');
    },
    
    // Prompt 弹窗
    prompt(message, title = '输入', defaultValue = '') {
        return this.show(title, message, 'prompt', defaultValue);
    },
    
    // 成功提示（绿色）
    success(message, title = '成功') {
        this.init();
        const modalConfirm = document.getElementById('modal-confirm');
        modalConfirm.classList.remove('bg-blue-600', 'hover:bg-blue-700', 'bg-red-600', 'hover:bg-red-700');
        modalConfirm.classList.add('bg-green-600', 'hover:bg-green-700');
        const result = this.show(title, message, 'alert');
        // 恢复默认颜色
        setTimeout(() => {
            modalConfirm.classList.remove('bg-green-600', 'hover:bg-green-700');
            modalConfirm.classList.add('bg-blue-600', 'hover:bg-blue-700');
        }, 500);
        return result;
    },
    
    // 错误提示（红色）
    error(message, title = '错误') {
        this.init();
        const modalConfirm = document.getElementById('modal-confirm');
        modalConfirm.classList.remove('bg-blue-600', 'hover:bg-blue-700', 'bg-green-600', 'hover:bg-green-700');
        modalConfirm.classList.add('bg-red-600', 'hover:bg-red-700');
        const result = this.show(title, message, 'alert');
        // 恢复默认颜色
        setTimeout(() => {
            modalConfirm.classList.remove('bg-red-600', 'hover:bg-red-700');
            modalConfirm.classList.add('bg-blue-600', 'hover:bg-blue-700');
        }, 500);
        return result;
    },
    
    // 警告提示（橙色）
    warning(message, title = '警告') {
        this.init();
        const modalConfirm = document.getElementById('modal-confirm');
        modalConfirm.classList.remove('bg-blue-600', 'hover:bg-blue-700', 'bg-red-600', 'hover:bg-red-700', 'bg-green-600', 'hover:bg-green-700');
        modalConfirm.classList.add('bg-orange-600', 'hover:bg-orange-700');
        const result = this.show(title, message, 'confirm');
        // 恢复默认颜色
        setTimeout(() => {
            modalConfirm.classList.remove('bg-orange-600', 'hover:bg-orange-700');
            modalConfirm.classList.add('bg-blue-600', 'hover:bg-blue-700');
        }, 500);
        return result;
    }
};

// 页面加载时初始化
if (typeof window !== 'undefined') {
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => {
            try {
                Modal.init();
            } catch (e) {
                console.error('Modal init error:', e);
            }
        });
    } else {
        try {
            Modal.init();
        } catch (e) {
            console.error('Modal init error:', e);
        }
    }
}
