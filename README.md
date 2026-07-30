<div align="center">
  <a href="https://v2.nonebot.dev/store"><img src="https://github.com/A-kirami/nonebot-plugin-template/blob/resources/nbp_logo.png" width="180" height="180" alt="NoneBotPluginLogo"></a>
  <br>
  <p><img src="https://github.com/A-kirami/nonebot-plugin-template/blob/resources/NoneBotPlugin.svg" width="240" alt="NoneBotPluginText"></p>
</div>

<div align="center">

# nonebot-plugin-uppic

_✨ 一个发送指令就能让你的 bot 发出对应指令的图片的插件 ✨_


<a href="./LICENSE">
    <img src="https://img.shields.io/github/license/HuParry/nonebot-plugin-uppic.svg" alt="license">
</a>
<a href="https://pypi.python.org/pypi/nonebot-plugin-uppic">
    <img src="https://img.shields.io/pypi/v/nonebot-plugin-uppic.svg" alt="pypi">
</a>
<img src="https://img.shields.io/badge/python-3.8+-blue.svg" alt="python">

</div>

## 📖 介绍

这是由 [nonebot-plugin-capoo](https://github.com/HuParry/nonebot-plugin-capoo) 插件衍生而来的随机发送图片的插件，从使用上来看，这个插件远优于 [nonebot-plugin-capoo](https://github.com/HuParry/nonebot-plugin-capoo) 插件。

你可以很方便地直接通过在配置文件里设置好触发指令（支持多个触发指令）后，在群聊中发送对应指令，从而使 Bot 随机发送出你所存储的图片。

插件除了能随机发送图片，还能在群聊内直接通过指令存储图片、删除图片，同时为了避免重复加入同一张图片，该插件添加了图片感知哈希算法，相似的图片无法重复添加。插件还支持完善的权限管理机制，超级用户可以灵活配置各群的上传权限。

随机发送图片是利用 sql 语句随机选择指令。

该插件的运行逻辑是：
- 自动在 Bot 所设路径下创建一个 `uppic` 文件夹；
- 在你预设指令（可以是多个）后，插件会检索指令，并在 `uppic/img` 目录下生成所有你预设命令作为名字的文件夹，同时在 `uppic/database` 目录下生成一个 `data.db` 的数据库文件；
- 之后你可以通过 Bot 存储图片，也可以直接在 `<预设命令>` 文件夹下存储图片。

例如你设置了其中一个触发指令为`capoo`，并且你设置了存储路径为`/data`（若不设置存储路径，则由 [nonebot-plugin-localstore](https://github.com/nonebot/plugin-localstore) 来指定存储路径），你可以自己直接给bot运行的服务器的`/data/uppic/img/capoo/`文件夹上传你想要的图片，并且你在群聊中通过指令让bot存储的图片也会存储在`/data/uppic/img/capoo/`里。

你可以在bot未启动期间任意修改图片文件夹内的所有内容，因为在启动bot后插件会自动做图片配置检查，因此你不用担心自己修改图片后会导致数据库与文件夹内容不同步。

**由于发送大储存的图片对于服务器来说压力很大，因此，图片大于1MB的将压缩至1MB以内再储存，请有高清图片保存需求的慎用！！！（毕竟发在群里的图片，画质太高也是浪费嘛）**

**在启动bot后不建议修改、删除以及添加图片**，否则可能会造成一些未知bug。

不建议你存储过多图片（指几万张甚至更多），因为没做过存储大量图片的测试。




### 静态网页生成的方案

图片太多太杂，上服务器查看图片太过麻烦，你是不是想通过网页直接看到目前已经存储了哪些图片？自v1.0.0版本后，本插件可以配置生成静态网页，目前已有两种生成方案。

#### fastapi生成静态网页
Bot启动后，会尝试构建静态网页，如果drivers中没有配置fastapi，将无法生效。

启动完成后，访问 http://\<hots\>:\<port\>/uppic 即可查看到内容。

#### 阿里云OSS对象存储

**适用对象**：1、服务器配置较低的用户：部署一个Bot后，再拉起一个web服务，服务器吃不消；2、服务器没有公网IP的用户：没有公网IP的话，在服务器上拉起web服务，其他人也无法访问（内网还是能访问的）。

配置阿里云OSS对象存储，如果你自己手里有已备案的域名，可以用来搭建静态网页，方便 自己/群友 点击链接查看图片列表。

如何配置阿里云OSS对象存储？请自行百度搜索步骤。

想要在插件里启用阿里云OSS功能，你需要准备好以下数据，便于之后配置到.env文件中：

- uppic_endpoint：自定义域名。你的OSS bucket捆绑的哪个域名就填哪个。注意尾部不加`/`；
- uppic_bucket：阿里云OSS存储空间的名称；
- uppic_region：bucket所在的地域，例如 `cn-beijing`，各个地域ID可见[OSS地域和访问域名](https://help.aliyun.com/zh/oss/regions-and-endpoints#e583bfe5e6sme)；
- uppic_oss_access_key_id、uppic_oss_access_key_secret：阿里云用户的AccessKey ID和AccessKey Secret。

以上的配置项不配齐的话静态网页功能将**不生效**。

bucket的访问权限至少要设置为公共读，否则通过域名也无法访问到图片。为了能网页访问，你需要给bucket配置好你自己的域名。

另外还需要如下图这样设置bucket的静态页面，跟下图一样地配置即可：

![](./docs/static-page.png)

注意：静态页面要通过 `上传oss` 指令后才会构建页面，但是每次通过 `添加` 指令添加图片时，无需重复发送 `上传oss` 指令，图片会自动上传。构建静态页面的时间可能比较长，因此要尽可能减少构建频率。

若有不明白的地方可以尝试联系我。




## 💿 安装

<details>
<summary>使用 nb-cli 安装</summary>
在 nonebot2 项目的根目录下打开命令行, 输入以下指令即可安装

    nb plugin install nonebot-plugin-uppic

</details>

<details>
<summary>使用包管理器安装</summary>
在 nonebot2 项目的插件目录下, 打开命令行, 根据你使用的包管理器, 输入相应的安装命令

<details>
<summary>pip</summary>

    pip install nonebot-plugin-uppic
</details>
<details>
<summary>pdm</summary>

    pdm add nonebot-plugin-uppic
</details>
<details>
<summary>poetry</summary>

    poetry add nonebot-plugin-uppic
</details>
<details>
<summary>conda</summary>

    conda install nonebot-plugin-uppic
</details>

打开 nonebot2 项目根目录下的 `pyproject.toml` 文件, 在 `[tool.nonebot]` 部分追加写入

    plugins = ["nonebot_plugin_capoo"]

</details>

## ⚙️ 配置

在 nonebot2 项目的`.env`文件中添加下表中的必填配置，非必填配置不添加也能正常使用。

|              配置项              | 必填 |                  默认值                   |                   说明                    |
|:-----------------------------:|:--:|:--------------------------------------:|:---------------------------------------:|
|    uppic_store_dir_path     | 否  | get_data_dir("nonebot_plugin_uppic") | 图片存储的路径，用户自定义路径，不定义路径则由localstore插件定义路径 |
|     uppic_banner_group      | 否  |                   []                   |               不触发发图功能的群聊                |
|       uppic_endpoint        | 否  |                  None                  |       填写自定义域名，域名尾部不用加/ （后续实现相关功能）       |
|        uppic_bucket         | 否  |                  None                  |         阿里云OSS对象存储空间名称(bucket)          |
|        uppic_region         | 否  |                  None                  |          阿里云OSS对象存储bucket所在地域           |
|   uppic_oss_access_key_id   | 否  |                  None                  |            阿里云用户AccessKey ID            |
| uppic_oss_access_key_secret | 否  |                  None                  |          阿里云用户AccessKey Secret          |
|  uppic_oss_no_upload_list   | 否  |                   []                   |    不上传到OSS的指令文件夹列表，该列表中对应的指令均不上传至OSS    |
|    uppic_super_users        | 否  |                   []                   |          超级用户列表，可跨群管理上传权限           |

在插件的本地存储目录下会有 `uppic_commands.json` 文件，该文件存储了对应指令以及每个指令的分群禁用配置。群里添加新指令、`@bot 禁用<指令>`、`@bot 启用<指令>` 都会修改这个文件，你也可以自己改完后重启 bot。

文件结构是「指令名 → 在该群号集合中被禁用」，例如：

```json
{
  "capoo": [],
  "马哥": [123456789]
}
```

上面表示 `capoo` 指令对所有群启用，`马哥` 指令仅在群号 `123456789` 中被禁用。

> 旧版（v1.4.0 及更早）的纯数组格式 `["capoo", "马哥"]` 仍可识别：插件启动时会自动迁移为上述字典格式并回写文件。

在 `.env` 中，例如这样配置（这是例子，不代表你也需要这样配置）：
```
uppic_store_dir_path="data/uppic"
uppic_banner_group=[574145050]
uppic_endpoint="https://xxx.huparry.cn"
uppic_bucket="uppic"
uppic_region="cn-beijing"
uppic_oss_access_key_id="xxxxxxxxxxxxxxxxxxxxxx"
uppic_oss_access_key_secret="xxxxxxxxxxxxxxxxxxxxx"
uppic_oss_no_upload_list=["capoo"]
```



## 🎉 使用

### 指令表

#### 基础指令
|     指令     | 权限 | 需要@ | 范围 |              说明               |
|:----------:|:--:|:---:|:--:|:-----------------------------:|
| `<你设置的指令>` | 群员 |  否  | 群聊 |         随机发送一张对应指令的图片         |
|  `添加<指令>`  | 群管/群主/授权用户 |  否  | 群聊 |       让 bot 存储图片到对应文件夹下       |
|  `删除图片<指令>`  | 群管/群主 |  否  | 群聊 |       分页预览并删除指定指令下的图片       |
|  `禁用<指令>`  | 群管/群主 |  是  | 群聊 |   在本群禁用该指令，之后该指令在本群将不再触发发图    |
|  `启用<指令>`  | 群管/群主 |  是  | 群聊 |        恢复本群被禁用的指令         |
|  `上传oss`   | 群管/群主 |  否  | 群聊 | 若配置了阿里云OSS，则通过该指令生成静态网页上传至OSS |

#### 超级用户指令（需配置 uppic_super_users）
|     指令     | 权限 | 需要@ |              说明               |
|:----------:|:--:|:---:|:-----------------------------:|
| `上传权限 <群号> admin_only` | 超级用户 |  是  | 设置该群仅群管可上传图片 |
| `上传权限 <群号> all_members` | 超级用户 |  是  | 设置该群所有群员可上传图片 |
| `添加上传用户 <群号> <用户ID>` | 超级用户 |  是  | 为指定群友单独授予上传权限 |
| `移除上传用户 <群号> <用户ID>` | 超级用户 |  是  | 移除指定群友的上传权限 |

### 删除图片流程
```
发送「删除图片<指令>」→ 输入页码 → 查看该页图片 → 输入序号删除 → 删除完成
```

**示例**：
```
删除图片capoo
→ 删除图片「capoo」，共 12 张图片，共 3 页。请输入页码（1-3）
→ 2
→ === 第 2 页 / 共 3 页 ===
→ 序号 5: [图片5]
→ 序号 6: [图片6]
→ 序号 7: [图片7]
→ 序号 8: [图片8]
→ 请输入要删除的序号（5-8）
→ 6 8
→ 成功删除 2 张图片！
```

### 权限配置说明

插件支持灵活的上传权限管理：

- **默认权限**：仅群管/群主可上传图片
- **超级用户**：可跨群管理上传权限，在任何群都能上传图片
- **分群权限**：可设置单个群为 `admin_only`（仅群管）或 `all_members`（全员）
- **指定用户**：超级用户可单独为特定群友授予上传权限

权限配置文件 `uppic_permissions.json`：
```json
{
  "123456789": {
    "mode": "admin_only",
    "allowed_users": [10001, 10002]
  }
}
```


## TODO
- [x] 指令触发 bot 发送图片
- [x] 在 QQ 上让 bot 存储对应指令的图片
- [x] 每次存储图片，判断相似图片是否已经存在，避免重复加入
- [X] 由capoo插件衍生成一个模板插件，即仅需修改参数就能发送别的图片
- [X] 添加图片压缩功能，避免图片文件夹空间过大
- [X] 以阿里云OSS对象存储搭建静态网页，便于浏览器查看图片
- [X] Bot使用期间也能在群内直接添加新指令。
- [X] 完善权限管理机制，采用更详细的权限管理，避免插件滥用
- [X] 添加删除图片功能，支持分页预览和批量删除
- [ ] 添加智能识图选项（调用api），避免添加违禁图导致被风控

## 鸣谢
感谢以下开发者作出的贡献：

<a href="https://github.com/HuParry/nonebot-plugin-randpic/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=HuParry/nonebot-plugin-randpic"  alt="contributors"/>
</a>