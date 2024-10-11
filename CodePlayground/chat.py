# 这里是一个 Toy Chat，祝你玩的开心，这里没有显卡要求
# 出现错误欢迎提出 Issue，记得贴上操作系统环境和可复现的命令。
# 对应文章：《19a. 从加载到对话：使用 Transformers 本地运行量化 LLM 大模型（GPTQ & AWQ）》
# https://github.com/Hoper-J/AI-Guide-and-Demos-zh_CN/blob/master/Guide/19a.%20从加载到对话：使用%20Transformers%20本地运行量化%20LLM%20大模型（GPTQ%20%26%20AWQ）.md
# 以及文章：《19b. 从加载到对话：使用 Llama-cpp-python 本地运行量化 LLM 大模型（GGUF）》
# https://github.com/Hoper-J/AI-Guide-and-Demos-zh_CN/blob/master/Guide/19b.%20从加载到对话：使用%20Llama-cpp-python%20本地运行量化%20LLM%20大模型（GGUF）.md
# 使用方法：python chat.py <model_path> [可选参数]
# 示例：python chat.py neuralmagic/Mistral-7B-Instruct-v0.3-GPTQ-4bit
# 查看完整帮助：使用 -h 或 --help

# ===== 严格检查所有的环境并给出安装指引，如果仅下载了 chat.py，注释以下两行来避免 utils 的报错 ======
from utils.environment_manager import EnvironmentManager
EnvironmentManager("utils/environment.yaml").setup_chat()
# ===== 环境检查 ======

import argparse
import json
import os
import sys
import warnings

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, TextStreamer
from transformers import logging as transformers_logging
from llama_cpp import Llama

from utils.config_manager import load_config

# 设置全局设备
DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'


class ChatSession:
    def __init__(self, messages=None, no_stream=False, history_path=None, output_path=None):
        """
        初始化对话会话。

        参数:
        - messages: 已加载的对话历史，默认为空列表。
        - no_stream (bool): 是否禁用流式输出。
        - history_path (str): 对话历史文件的路径。
        - output_path (str): 对话历史保存的文件路径。
        """
        self.messages = messages or self.load_history(history_path)
        self.no_stream = no_stream
        self.output_path = output_path

    def load_history(self, history_path):
        """
        加载对话历史记录。

        参数:
        - history_path (str): 对话历史文件的路径。

        返回:
        - list: 已加载的对话历史，默认为空列表。
        """
        if history_path and os.path.exists(history_path):
            try:
                with open(history_path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    if not content:
                        print(f"文件 '{history_path}' 内容为空，开始新的对话历史。")
                        return []
                    messages = json.loads(content)
                print(f"已加载对话历史记录：{history_path}")
                return messages
            except (json.JSONDecodeError, IOError) as e:
                print(f"加载对话历史时出错: {e}，开始新的对话历史。")
        return []

    def add_message(self, role, content):
        """
        添加一条消息到会话中。

        参数:
        - role (str): 消息角色，通常为 'user' 或 'assistant'。
        - content (str): 消息内容。
        """
        self.messages.append({"role": role, "content": content})

    def save_history(self):
        """
        保存对话历史到文件。
        """
        if self.output_path:
            try:
                with open(self.output_path, 'w', encoding='utf-8') as f:
                    json.dump(self.messages, f, ensure_ascii=False, indent=2)
                print(f"对话历史已保存至: {self.output_path}")
            except Exception as e:
                print(f"保存对话历史时出错: {e}")

    def handle_user_input(self):
        """
        处理用户输入，包括捕获 EOF 输入和退出命令。

        返回:
        - str: 用户输入的文本。
        - bool: 是否应该终止会话。
        """
        try:
            user_input = input("User: ").strip()
        except EOFError:
            print("\n收到 EOF 输入，正在退出对话...")
            return "", True  # 返回终止信号

        if user_input.lower() in ["exit", "quit", "bye"]:
            print("Goodbye!")
            return "", True  # 返回终止信号

        return user_input, False
            
    def get_response(self, user_input):
        """
        这是一个抽象方法，需要在子类中实现。

        参数:
        - user_input (str): 用户输入的文本。
        """
        raise NotImplementedError("子类必须实现 get_response 方法。")

    def start(self):
        """
        启动对话会话，处理输入和响应，直到用户退出。
        """
        print("开始对话，输入 'exit'、'quit' 或 'bye' 结束对话，或者使用 Ctrl+D (EOF) 退出。")
        while True:
            user_input, should_terminate = self.handle_user_input()
            if should_terminate:
                self.save_history()
                break

            self.get_response(user_input)


class LlamaChatSession(ChatSession):
    def __init__(self, llm, max_length=200, no_stream=False, history_path=None, output_path=None):
        """
        初始化 Llama 对话会话。

        参数:
        - llm: Llama 模型实例。
        - max_length (int): 生成文本的最大长度。
        - no_stream (bool): 是否禁用流式输出。
        - history_path (str): 对话历史文件的路径。
        - output_path (str): 对话历史保存的文件路径。
        """
        super().__init__(no_stream=no_stream, history_path=history_path, output_path=output_path)
        self.llm = llm
        self.max_length = max_length

    def get_response(self, user_input):
        """
        获取 Llama 模型对用户输入的响应（支持流式输出）。

        参数:
        - user_input (str): 用户输入的文本。

        返回:
        - response (str): 完整的回复文本。
        """
        # 添加用户消息时，确保对话角色交替，避免可能的报错：
        # jinja2.exceptions.TemplateError: Conversation roles must alternate user/assistant/user/assistant/...
        if len(self.messages) == 0 or self.messages[-1]["role"] == "assistant":
            self.add_message("user", user_input)
        else:
            print("Warning: Detected consecutive user messages, adjusting conversation flow.")
            self.messages.append({"role": "assistant", "content": ""})
            self.add_message("user", user_input)

        try:
            output = self.llm.create_chat_completion(
                messages=self.messages,
                max_tokens=self.max_length,
                stream=not self.no_stream
            )
            
            if not self.no_stream:
                # 流式输出处理
                response = self._handle_stream_output(output)
            else:
                # 非流式输出处理，从 output 中提取完整回复
                response = output['choices'][0]['message']['content']
                print(response)  # 直接打印生成的内容
            
            # 添加回复到对话历史
            self.add_message("assistant", response.strip())
            return response.strip()
        except Exception as e:
            print(f"\n发生错误: {e}")

    def _handle_stream_output(self, output):
        """
        处理流式输出，将生成的内容逐步打印出来，并收集完整的回复。
    
        参数：
            output: 生成器对象，来自 create_chat_completion 的流式输出。
    
        返回：
            response: 完整的回复文本。
        """
        response = ""
        for chunk in output:
            delta = chunk['choices'][0]['delta']
            if 'role' in delta:
                print(f"{delta['role']}: ", end='', flush=True)
            elif 'content' in delta:
                content = delta['content']
                print(content, end='', flush=True)
                response += content
        print()  # 在输出结束时自动换行
        return response


class TransformersChatSession(ChatSession):
    def __init__(self, model, tokenizer, max_length=200, no_stream=False, history_path=None, output_path=None):
        """
        初始化 Transformers 对话会话。

        参数:
        - model: Transformers 模型实例。
        - tokenizer: 模型的分词器。
        - max_length (int): 生成文本的最大长度。
        - no_stream (bool): 是否禁用流式输出。
        - history_path (str): 对话历史文件的路径。
        - output_path (str): 对话历史保存的文件路径。
        """
        super().__init__(no_stream=no_stream, history_path=history_path, output_path=output_path)
        self.model = model
        self.tokenizer = tokenizer
        self.max_length = max_length

    def get_response(self, user_input):
        """
        获取 Transformers 模型对用户输入的响应（支持流式输出）。

        参数:
        - user_input (str): 用户输入的文本。

        返回:
        - response (str): 完整的回复文本。
        """
        # 添加用户消息时，确保对话角色交替，避免可能的报错：
        # jinja2.exceptions.TemplateError: Conversation roles must alternate user/assistant/user/assistant/...
        if len(self.messages) == 0 or self.messages[-1]["role"] == "assistant":
            self.add_message("user", user_input)
        else:
            print("Warning: Detected consecutive user messages, adjusting conversation flow.")
            self.messages.append({"role": "assistant", "content": ""})
            self.add_message("user", user_input)

        input_ids = self.tokenizer.apply_chat_template(self.messages, return_tensors="pt").to(DEVICE)

        streamer = TextStreamer(
            self.tokenizer, 
            skip_prompt=True,
            skip_special_tokens=True
        ) if not self.no_stream else None

        generation_kwargs = {
            "input_ids": input_ids,
            "max_length": len(input_ids[0]) + self.max_length,
            "streamer": streamer,
            "pad_token_id": self.tokenizer.eos_token_id
        }

        with torch.no_grad():
            print("Assistant: ", end="")
            output_ids = self.model.generate(**generation_kwargs)

        assistant_reply = self.tokenizer.decode(output_ids[0][input_ids.shape[1]:], skip_special_tokens=True)
        
        self.add_message("assistant", assistant_reply)
        if self.no_stream:
            print(assistant_reply)


def create_chat_session(model_name_or_path, max_length, no_stream, history_path, output_path):
    """
    根据模型路径和类型创建适当的 ChatSession 实例。

    参数:
    - model_name_or_path (str): 模型的名称或本地路径。
    - max_length (int): 生成文本的最大长度。
    - no_stream (bool): 是否禁用流式输出。
    - history_path (str): 对话历史的输入文件路径（可选）。
    - output_path (str): 对话历史的保存路径。

    返回:
    - ChatSession: 适当的 ChatSession 实例（LlamaChatSession 或 TransformersChatSession）。
    """
    print(f"正在加载模型: {model_name_or_path}")
    is_gguf = model_name_or_path.endswith('.gguf')

    try:
        if is_gguf:
            # 加载 GGUF 格式模型并创建 LlamaChatSession
            llm = Llama(
                model_path=model_name_or_path,
                n_gpu_layers=-1,  # 根据需要卸载到 GPU
                n_ctx=4096,       # 设置上下文窗口大小
                verbose=False
            )
            print(f"GGUF 模型加载完成，路径: {model_name_or_path}")
            return LlamaChatSession(
                llm=llm, 
                max_length=max_length, 
                no_stream=no_stream, 
                history_path=history_path,
                output_path=output_path
            )
        else:
            # 加载 Transformers 模型并创建 TransformersChatSession
            tokenizer = AutoTokenizer.from_pretrained(model_name_or_path, trust_remote_code=True)
            model = AutoModelForCausalLM.from_pretrained(
                model_name_or_path,
                torch_dtype="auto",
                device_map="auto" if DEVICE == 'cuda' else None
            ).to(DEVICE)
            print(f"模型加载完成，当前使用设备: {DEVICE}。")
            if DEVICE == 'cuda':
                print(f"当前显存占用: {torch.cuda.memory_allocated() / 1e6:.2f} MB")
            return TransformersChatSession(
                model=model,
                tokenizer=tokenizer,
                max_length=max_length,
                no_stream=no_stream,
                history_path=history_path,
                output_path=output_path
            )
    except Exception as e:
        print(f"加载模型失败: {e}")
        raise


def configure_logging(verbose):
    """
    配置日志输出和警告过滤。

    参数:
    - verbose (bool): 是否启用详细日志。
    """
    if verbose:
        # 启用详细输出
        transformers_logging.set_verbosity_info()
        warnings.filterwarnings("default")
    else:
        # 禁用 Transformers 库的详细日志
        transformers_logging.set_verbosity_error()
        # 忽略所有警告
        warnings.filterwarnings("ignore")


def main():
    # 加载配置文件中 'chat' 的默认配置
    config = load_config(script_name='chat')
    
    # 设置命令行参数
    parser = argparse.ArgumentParser(
        description="本地加载量化 LLM 模型并进行对话，该脚本可以自动从 Hugging Face 下载量化模型",
        epilog="示例: python chat.py neuralmagic/Mistral-7B-Instruct-v0.3-GPTQ-4bit"
    )
    
    parser.add_argument("path", type=str, help="模型的名称或本地路径")
    parser.add_argument("--max_length", "-m", type=int, default=config.get('max_length', 512), help="生成文本的最大长度")
    parser.add_argument("--no_stream", action="store_true", default=config.get('no_stream', False), help="禁用流式输出")
    parser.add_argument("--history_path", "-i", type=str, default=config.get('history_path'), help="对话历史的输入文件路径（可选）")
    parser.add_argument("--output_path", "-o", type=str, default=config.get('output_path'), help="对话历史的输出文件路径（可选）")
    parser.add_argument("--io", "-io", type=str, help="同时指定对话历史输入和输出的路径")
    parser.add_argument("--verbose", "-v", action="store_true", help="启用详细日志输出")
    args = parser.parse_args()

    # 配置日志和警告
    configure_logging(args.verbose)
    
    # 如果用户提供了 -io 参数，则同步设置 history_path 和 output
    if args.io:
        args.history_path = args.io
        args.output_path = args.io

    # 创建 ChatSession 实例
    try:
        chat_session = create_chat_session(
            model_name_or_path=args.path,
            max_length=args.max_length,
            no_stream=args.no_stream,
            history_path=args.history_path,
            output_path=args.output_path
        )
    except Exception as e:
        print(f"创建对话会话失败: {e}")
        sys.exit(1)

    # 启动对话
    chat_session.start()


if __name__ == "__main__":
    main()