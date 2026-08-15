FROM --platform=amd64 kalilinux/kali-rolling:latest

# apt 源显式固定（基础镜像把源放在 /etc/apt/sources.list.d/*.sources，其中东软镜像
# mirrors.neusoft.edu.cn 曾 DNS 不可达导致 kali-linux-headless 安装失败）。
# 默认 Kali 官方 CDN（CI/托管稳定）；国内构建可用 --build-arg KALI_MIRROR=清华/阿里镜像提速。
ARG KALI_MIRROR="http://http.kali.org/kali"
RUN rm -f /etc/apt/sources.list.d/* && \
    echo "deb ${KALI_MIRROR} kali-rolling main non-free contrib" > /etc/apt/sources.list && \
    apt update && \
    (apt -y install kali-linux-headless || (sleep 5 && apt -y install kali-linux-headless)) && \
    apt clean && \
    rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash kali && \
    echo "kali ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers
USER kali

RUN sudo apt update && \
    sudo apt install -y bsdextrautils npm jq iputils-ping sshpass ncat rlwrap \
    bloodyad coercer enum4linux-ng pwncat chisel-common-binaries \
    dirsearch yq krb5-user gitleaks naabu nikto netexec adb && \
    sudo apt clean && \
    sudo rm -rf /var/lib/apt/lists/* && \
    pip3 install pwntools pymongo --break-system-packages && \
    sudo npm install -g @playwright/cli@latest && \
    cd /tmp && playwright-cli install

# 第三方二进制固定版本 + sha256 校验（供应链防护：上游 release 被替换/CDN 劫持即构建失败）。
# katana/nuclei 哈希取自官方 release checksums.txt；dalfox/cloudfox/kerbrute 上游不提供
# 校验文件，哈希为对官方 release 资产（尺寸已与 GitHub API 核对）本地 sha256 计算值。
RUN cd /tmp && \
    wget -q "https://github.com/projectdiscovery/katana/releases/download/v1.5.0/katana_1.5.0_linux_amd64.zip" -O katana.zip && \
    wget -q "https://github.com/projectdiscovery/nuclei/releases/download/v3.7.1/nuclei_3.7.1_linux_amd64.zip" -O nuclei.zip && \
    wget -q "https://github.com/hahwul/dalfox/releases/download/v2.12.0/dalfox-linux-amd64.tar.gz" -O dalfox.tar.gz && \
    wget -q "https://github.com/BishopFox/cloudfox/releases/download/v2.0.1/cloudfox-linux-amd64.zip" -O cloudfox.zip && \
    wget -q "https://github.com/ropnop/kerbrute/releases/download/v1.0.3/kerbrute_linux_amd64" -O kerbrute && \
    echo "592890e5febaf5706d0a962d96299512418d6eccce6388cf1600e1f078ed359d  katana.zip" | sha256sum -c - && \
    echo "72dbc77e87b3a6751adc151e514b15f51e2c94e493a561fc9e1138397c6e22c6  nuclei.zip" | sha256sum -c - && \
    echo "56ea86c91f04dd64c675897cc7c9317630d6b65e610b16a079ef25c964ec8ab0  dalfox.tar.gz" | sha256sum -c - && \
    echo "73b0cc7be28a4cdfbfabe7ef1e217e3c871624fc355f01689c12b63893506c3d  cloudfox.zip" | sha256sum -c - && \
    echo "710a9d2653c8bd3689e451778dab9daec0de4c4c75f900788ccf23ef254b122a  kerbrute" | sha256sum -c - && \
    git clone --depth 1 "https://github.com/projectdiscovery/nuclei-templates.git" /home/kali/.local/nuclei-templates && \
    unzip -q katana.zip katana && sudo mv katana /usr/local/bin/ && \
    unzip -q nuclei.zip nuclei && sudo mv nuclei /usr/local/bin/ && \
    tar -xzf dalfox.tar.gz && sudo mv dalfox-linux-amd64 /usr/local/bin/dalfox && \
    unzip -q cloudfox.zip cloudfox/cloudfox && sudo mv cloudfox/cloudfox /usr/local/bin/ && \
    sudo cp kerbrute /usr/local/bin/ && \
    nuclei -ut && \
    sudo rm -rf /tmp/*

RUN mkdir -p /home/kali/knowledges && cd /home/kali/knowledges && \
    git clone --depth=1 https://github.com/swisskyrepo/PayloadsAllTheThings.git && \
    git clone --depth=1 https://github.com/swisskyrepo/InternalAllTheThings.git && \
    git clone --depth=1 https://github.com/HackTricks-wiki/hacktricks.git && \
    git clone --depth=1 https://github.com/HackTricks-wiki/hacktricks-cloud.git

RUN mkdir -p /home/kali/tools && cd /home/kali/tools && \
    wget -q "https://github.com/frohoff/ysoserial/releases/download/v0.0.6/ysoserial-all.jar" -O ysoserial.jar && \
    echo "2c9bddd6a1a4ec66c1078ea97dacb61eb66d1c41aec7b6d21e3c72214ce170f1  ysoserial.jar" | sha256sum -c - && \
    git clone --depth=1 https://github.com/ticarpi/jwt_tool.git && \
    cd jwt_tool && pip3 install -r requirements.txt --break-system-packages && \
    git clone --depth=1 https://github.com/IOActive/jdwp-shellifier.git

RUN mkdir -p /home/kali/pocs && cd /home/kali/pocs && \
    git clone --depth=1 https://github.com/iSee857/CVE-PoC.git && \
    git clone --depth=1 https://github.com/zhzyker/exphub.git && \
    git clone --depth=1 https://github.com/passwa11/2023Hvv_.git && \
    git clone --depth=1 https://github.com/Threekiii/Awesome-POC.git && \
    git clone --depth=1 https://github.com/vulhub/vulhub.git

RUN cd /tmp && \
    wget -q "https://github.com/BurntSushi/ripgrep/releases/download/15.1.0/ripgrep_15.1.0-1_amd64.deb" -O ripgrep.deb && \
    wget -q "https://github.com/sharkdp/fd/releases/download/v10.4.2/fd_10.4.2_amd64.deb" -O fd.deb && \
    echo "62ce186675c71edbc7c48a67dc36f986974551354d6ce63726adc2d85aad802c  ripgrep.deb" | sha256sum -c - && \
    echo "0e44eb5fca93f09bc6f5430b90acdf44c8e069d0a903700aeb4820629337b67b  fd.deb" | sha256sum -c - && \
    sudo apt install ./ripgrep.deb ./fd.deb && \
    sudo rm -rf /tmp/* && \
    sudo apt clean && \
    sudo rm -rf /var/lib/apt/lists/*

# aliyun CLI：弃用 pipe-to-bash 安装脚本，改为固定版本 release + sha256 校验
RUN cd /tmp && \
    wget -q "https://github.com/aliyun/aliyun-cli/releases/download/v3.4.11/aliyun-cli-linux-3.4.11-amd64.tgz" -O aliyun-cli.tgz && \
    echo "a7e3df497db14c10d4d7587795e9fa7849b0c51dfce02908b9de5a41fe717d5c  aliyun-cli.tgz" | sha256sum -c - && \
    tar -xzf aliyun-cli.tgz && sudo mv aliyun /usr/local/bin/aliyun && \
    rm -f /tmp/aliyun-cli.tgz

RUN pip3 install tccli awscli --break-system-packages

# astra 引擎（server + dispatcher）与靶场接入编排器
COPY ./astra /opt/astra
RUN cd /opt/astra && pip3 install uv_build --break-system-packages && pip3 install . --break-system-packages
RUN pip3 install tsec-benchmark --break-system-packages
COPY ./container/astra_runner /opt/astra-runner
# ASTRA 定制的 dsh headless 扩展（--session 会话续接，见 container/dsh/README.md）
COPY ./container/dsh /opt/astra/dsh

# 镜像默认入口：astra-runner（本地模式可手动执行 python3 /opt/astra-runner/runner.py）
ENTRYPOINT ["python3", "/opt/astra-runner/runner.py"]

RUN sudo npm install -g @openai/codex@0.118.0
RUN sudo npm install -g @anthropic-ai/claude-code@2.1.98
RUN sudo npm install -g @mariozechner/pi-coding-agent@0.73.0
# DeepSeek Harness 无头模式 + ASTRA runner 插件（复制进 dsh 包 lib/，供 --patch 以裸说明符加载）
RUN sudo npm install -g @deepseek-ai/dsh@0.1.0-rc.6 && \
    sudo cp /opt/astra/dsh/astra-headless-runner.js "$(sudo npm root -g)/@deepseek-ai/dsh/lib/astra-headless-runner.js"
# Playwright MCP（headless 通过 mcp-client 接入，Web 类题目用真实浏览器；npx -y 现场下载的替代）
RUN sudo npm install -g @playwright/mcp@latest

RUN sudo chmod +x /usr/local/bin/kerbrute
RUN mkdir -p /home/kali/.config/nuclei && \
    echo >> /home/kali/.config/nuclei/config.yaml && \
    echo "disable-update-check: true" >> /home/kali/.config/nuclei/config.yaml && \
    echo "update-template-dir: /home/kali/.local/nuclei-templates" >> /home/kali/.config/nuclei/config.yaml && \
    rm -rf /home/kali/nuclei-templates
RUN sudo rm -rf /usr/lib/llvm-*/build && \
    sudo chmod o+rx /usr/lib/mysql/plugin/auth_pam_tool_dir /etc/ssl/private
RUN printf '%s\n' 'PS1="${PS1%\\n*} \[\033[0;36m\]\D{%y-%m-%d} - \t\[\033[;32m\]\n${PS1##*\\n}"' >> /home/kali/.bashrc && \
    mkdir -p /home/kali/workspace && cd /home/kali/workspace && git init
ENV TZ=Asia/Shanghai
ENV PLAYWRIGHT_MCP_BROWSER=chromium
ENV CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
ENV PYTHONUNBUFFERED=1

COPY ./container/.agents /home/kali/workspace/.agents
COPY ./container/.agents /home/kali/workspace/.claude
COPY ./container/AGENTS.md /home/kali/workspace/AGENTS.md
COPY ./container/AGENTS.md /home/kali/workspace/CLAUDE.md

WORKDIR /home/kali/workspace