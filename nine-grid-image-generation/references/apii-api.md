# GPT Image 2.0 4K API 接口文档

模型名称：`gpt-image-2.0-4k`
模型说明：顶级质量 原生4k渠道，嘎嘎好用，嘎嘎划算~~~

API Base URL：`https://ai.apii.cn`

## 鉴权

所有请求均需携带请求头：

```
Authorization: Bearer 你的APIKey
Content-Type: application/json
```

## 接口

- 文生图：`POST /v1/images/generations`
- 参考图编辑：`POST /v1/images/edits`

## 请求参数

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| model | string | 是 | 模型名称，填写 gpt-image-2.0-4k |
| prompt | string | 是 | 图像内容描述 |
| aspect_ratio | string | 否 | 图片比例，例如 16:9、4:3、1:1 或 9:16，具体可参考下方比例尺寸表；参数生效后会覆盖 size 的参数值 |
| size | string | 否 | 详见下方尺寸表 |
| quality | string | 否 | 图像质量：auto、low、medium 或 high |
| output_format | string | 否 | 图片格式：png、jpeg 或 webp |
| response_format | string | 否 | 返回格式：url 或 b64_json |

图片编辑时额外传入 `images`，类型为 Base64 或图片 URL 数组，支持多张图片。

## 比例与尺寸

填写有效的 `aspect_ratio` 后会覆盖 `size`。

| aspect_ratio | 对应 size |
| --- | --- |
| 16:9 | 3840x2160 |
| 21:9 | 3840x1648 |
| 4:3 | 3264x2448 |
| 3:2 | 3504x2336 |
| 5:4 | 3200x2560 |
| 1:1 | 2880x2880 |
| 4:5 | 2560x3200 |
| 2:3 | 2336x3504 |
| 3:4 | 2448x3264 |
| 9:16 | 2160x3840 |
| 9:21 | 1648x3840 |

自定义比例使用正整数 `n:m` 格式，长边与短边比例不得超过 3:1。宽高按 16px 的倍数计算，单边不超过 3840px，超出会等比缩小；总像素不超过 8294400。

## 文生图 cURL 示例

```bash
curl https://ai.apii.cn/v1/images/generations \
  -H "Authorization: Bearer 你的APIKey" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-image-2.0-4k",
    "prompt": "一张具有电影光影的高端香水产品海报",
    "aspect_ratio": "9:16",
    "quality": "high",
    "output_format": "png",
    "response_format": "url"
  }'
```

## 参考图编辑 cURL 示例（图片 URL）

```bash
curl https://ai.apii.cn/v1/images/edits \
  -H "Authorization: Bearer 你的APIKey" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-image-2.0-4k",
    "prompt": "将商品放入背景场景，并保持标签文字清晰",
    "images": ["https://tucdn.wpon.cn/2026/06/11/e2965f490445a-1781148062.png"],
    "aspect_ratio": "9:16",
    "quality": "high",
    "response_format": "url"
  }'
```

## 参考图编辑 cURL 示例（Base64）

```bash
curl https://ai.apii.cn/v1/images/edits \
  -H "Authorization: Bearer 你的APIKey" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-image-2.0-4k",
    "prompt": "将商品放入背景场景，并保持标签文字清晰",
    "images": ["图片的Base64字符串"],
    "aspect_ratio": "9:16",
    "quality": "high",
    "response_format": "url"
  }'
```

## JavaScript 示例

```javascript
const response = await fetch("https://ai.apii.cn/v1/images/generations", {
  method: "POST",
  headers: {
    "Authorization": "Bearer 你的APIKey",
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    model: "gpt-image-2.0-4k",
    prompt: "一张具有电影光影的高端香水产品海报",
    aspect_ratio: "9:16",
    quality: "high",
    response_format: "url",
  }),
});

const result = await response.json();
console.log(result.data[0].url);
```

## Python 示例

```python
import requests

response = requests.post(
    "https://ai.apii.cn/v1/images/generations",
    headers={"Authorization": "Bearer 你的APIKey"},
    json={
        "model": "gpt-image-2.0-4k",
        "prompt": "一张具有电影光影的高端香水产品海报",
        "aspect_ratio": "9:16",
        "quality": "high",
        "response_format": "url",
    },
)

result = response.json()
print(result["data"][0]["url"])
```

## URL 返回结构

```json
{
  "created": 1782105234,
  "data": [
    {
      "url": "https://example.com/generated-image.png"
    }
  ]
}
```

## Base64 返回结构

```json
{
  "created": 1782105449,
  "data": [
    {
      "b64_json": "图片的Base64字符串"
    }
  ]
}
```
