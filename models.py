from cgitb import text
from math import tan, tanh
from re import L
from scipy import constants
import torch
import torch.nn as nn
import torch.nn.functional as F
import clip
import matplotlib.pyplot as plt
import numpy as np
from collections import defaultdict
import json
from PIL import Image, ImageFile
from zmq import device
ImageFile.LOAD_TRUNCATED_IMAGES = True
import constants as _constants_
from torch import Tensor
from transformers import AutoModel, AutoTokenizer
import os
from torchvision import models
os.environ['CURL_CA_BUNDLE'] = ''

import requests.packages.urllib3
requests.packages.urllib3.disable_warnings()


device = "cuda" if torch.cuda.is_available() else "cpu"
class OrthogonalTextEncoder(nn.Module):
    def __init__(self, d_model=512):
        super().__init__()
        # Transformer编码器
        self.encoder_ly = nn.TransformerEncoderLayer(d_model, nhead=8, dim_feedforward=2048, dropout=0.1, batch_first=True)
        self.encoder = nn.TransformerEncoder(self.encoder_ly, num_layers = 8)

    def forward(self, x:Tensor) -> Tensor:
        '''
        expect x - [batch, sequence, dim]
        output y - [batch, sequence, dim]
        '''
        x = self.encoder(x)
        return x

class ImgClassifier(nn.Module):
    '''take LGCLIP model with linear heads for supervised classification on images -- positive/negative/uncertain.
    '''
    def __init__(self,
        img_branch,
        num_class:str,
        input_dim=512,
        mode='multiclass',
        **kwargs) -> None:
        '''args:
        vision_model: the LGCLIP vision branch model that encodes input images into embeddings.
        num_class: number of classes to predict
        input_dim: the embedding dim before the linear output layer
        mode: multilabel, multiclass, or binary
        input number:  the number of input embeddings
        num_class: corresponding with the number of disease --- the dim of output
        '''
        super(ImgClassifier, self).__init__()
        device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = img_branch.to(device)
        self.num_class = num_class
        assert mode.lower() in ['multiclass','multilabel','binary']
        self.mode = mode.lower()
        if num_class > 2:
            if mode == 'multiclass':
                self.loss_fn = nn.CrossEntropyLoss()
            else:
                self.loss_fn = nn.BCEWithLogitsLoss()

            self.fc = nn.Linear(input_dim, input_dim)
            self.cls = nn.Linear(input_dim, num_class)
        else:
            self.loss_fn = nn.BCEWithLogitsLoss()
            self.fc = nn.Linear(input_dim, 1)

    def forward(self,
        img_path,  ## original image
        labels=None,
        return_loss=True,
        **kwargs,
        ):

        assert labels is not None
        
        outputs = defaultdict()
        image_embeddings = image_embeddings.cuda().to("cpu")
        # take embeddings before the projection head
        img_embeds = self.model(img_path)
        logits = self.fc(img_embeds)
        logits = self.cls(logits)
        outputs['embedding'] = img_embeds
        outputs['logits'] = logits
        if labels is not None and return_loss:
            labels = labels.cuda().float()
            if len(labels.shape) == 1: labels = labels.view(-1,1)
            if self.mode == 'multiclass': labels = labels.flatten().long()
            loss = self.loss_fn(logits, labels)
            outputs['loss_value'] = loss
        return outputs

class SplitVisEncoder(nn.Module):
    def __init__(self, n, d_model=512, nhead = 8, layers = 6, hid_dim=2048, drop = 0.01):
        super(SplitVisEncoder, self).__init__()

        # 分割输入向量为n份
        self.n = n
        self.input_splits = nn.Linear(d_model, d_model * n)
        self.fc = nn.Linear(d_model * n, d_model * n)
        self.sequential = nn.Sequential(
            self.input_splits,
            self.fc
        )
        # Transformer编码器
        self.encoder_ly = nn.TransformerEncoderLayer(d_model, nhead=8, dim_feedforward=2048, dropout=0.1, batch_first=True)
        self.encoder = nn.TransformerEncoder(self.encoder_ly, num_layers = 8)

    def forward(self, x:Tensor, project=False):
        '''
        expect input shape x - [batch, dim]
        output shape y - [batch, n, dim]
        '''
        x = self.sequential(x) # 1 * n * dim
        x = x.view(x.size(0), self.n, -1)   #.permute(1, 0, 2)  # 调整形状为 (n, batch_size, d_model)

        # 通过Transformer编码器
        x = self.encoder(x)

        # 取每个时间步的输出
        # x = x.permute(1, 0, 2)

        return x.cuda()

class TextBranch(nn.Module):
    def __init__(self, text_embedding_dim = 512, num_transformer_heads = 8, num_transformer_layers = 6, proj_bias = False, nntype = None):
        super().__init__()
        # 初始化 CLIP 预训练模型和处理器
        self.projection_head = nn.Linear(512, 512, bias=False)
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        if nntype == None:
            self.backbone = "clip"
        else:
          self.backbone = nntype
        print(_constants_.BOLD + _constants_.BLUE + "in current Text branch, the text backbone for text embedding is: " + _constants_.RESET + self.backbone)            
       
        if self.backbone in ["biomed", "BiomedCLIP", "biomedclip"]:
            import open_clip
            self.clip_model, preprocess_train, preprocess_val = open_clip.create_model_and_transforms('hf-hub:microsoft/BiomedCLIP-PubMedBERT_256-vit_base_patch16_224')
            self.tokenizer = open_clip.get_tokenizer('hf-hub:microsoft/BiomedCLIP-PubMedBERT_256-vit_base_patch16_224')
        elif self.backbone == "custom":
            raise NotImplemented("has not implemented the custom backbone in text branch")
        else:
            ## the default backbone is CLIP -- text encoder
            self.clip_model, self.clip_processor  = clip.load("/public_bme/data/lds/model_zoo/ViT-B/32", device=self.device)
        # 冻结 CLIP 部分的参数
        if self.backbone != "custom":
          for param in self.clip_model.parameters():
              param.requires_grad = False
        # text orthogonal 部分
        self.transformer = OrthogonalTextEncoder()
        self.backbone = nntype
        
    def forward(self, text_inputs:list):
        '''
        文字分支：
        输入: b * [prompt1, prompt2, prompt3, ...]
        mid: b x n x 512
        输出: b x [n vectors corresponding with prompts]
        '''
        # 输入经过 CLIP 预训练模型
        # text_inputs = torch.cat([clip.tokenize(f"image of {c}") for c in text_inputs]).to(device)
        text_features = []
        # print(f'\033[31mthe type of text_inputs : {type(text_inputs)}\033[0m')
        if self.backbone != "custom":
          with torch.no_grad():
              for text_input in text_inputs:
                text_feature = torch.load(text_input).to(self.device)
                text_features.append(text_feature)                  
        text_features = torch.stack(text_features, dim = 0).squeeze().to(self.device)
        output = self.transformer(text_features)  ## ToDo 正则化处理
        return  output

      
class ImgBranch(nn.Module):
    def __init__(self, text_embedding_dim = 512, num_transformer_heads = 8, num_transformer_layers = 6, proj_bia = False, nntype = None, backbone_v:str = None):
        super().__init__()
        # 初始化 CLIP 预训练模型和处理器
        self.projection_head = nn.Linear(512, 512, bias=False)
        device = "cuda" if torch.cuda.is_available() else "cpu"
        
        self.VisEncoder = SplitVisEncoder(13, d_model=512, nhead = 8, layers = 6, hid_dim=2048, drop = 0.01).to(device)
        
        self.device = device
        if backbone_v == "densenet":
          self.backbone_v = self.densenet().to(device)
          self.backbone = "custom"
          self.backbone_n = backbone_v
          print(_constants_.BOLD + _constants_.BLUE + "in current image branch, the vis backbone for vis embedding is: " + _constants_.RESET + self.backbone_n)    
          return
        
        if nntype == None:
            self.backbone = "clip"
        else:
            self.backbone = nntype
        print(_constants_.BOLD + _constants_.BLUE + "in current image branch, the vis backbone for vis embedding is: " + _constants_.RESET + self.backbone)            
        
        if self.backbone in ["biomedCLIP", "biomed", "biomedclip",]:
            import open_clip
            self.clip_model, preprocess_train, self.clip_processor = open_clip.create_model_and_transforms('hf-hub:microsoft/BiomedCLIP-PubMedBERT_256-vit_base_patch16_224')
        elif self.backbone == "custom":
            raise NotImplemented("using custom vis backbone which has not be defined!!!!!")
        else:
            self.clip_model, self.clip_processor  = clip.load("/public_bme/data/lds/model_zoo/ViT-B-32.pt", device=device)
        #  in this case, Biomed and CLIP model are been frozen 
        for param in self.clip_model.parameters():
            param.requires_grad = False


    
    def densenet(self, n_dim = 512):
        model = models.densenet121(pretrained=True)
        num_ftrs = model.classifier.in_features
        model.classifier = nn.Sequential(nn.Linear(num_ftrs, n_dim))  
        return model
    def forward(self, image_path):
        '''
        input: img_path, str
        imd : b X 512
        output: b x n x 512
        '''
        images = []
        for image in image_path:
            if "/Users/liu/Desktop/school_academy/ShanghaiTech" in image:
                image = image.replace("/Users/liu/Desktop/school_academy/ShanghaiTech", "D://exchange//ShanghaiTech//")
            images.append(torch.load(image))

        image_input = torch.tensor(np.stack(images)).to(self.device)
        # 输入经过 CLIP 预训练模型
        if self.backbone != "custom":
          with torch.no_grad():
              image_features = self.clip_model.encode_image(image_input).float()
        else:
            # print("Using " + _constants_.RED + f"{self.backbone_n} as vision encoder" + _constants_.RESET + " in image branch" )
            image_features = self.backbone_v(image_input).float()
        output = self.VisEncoder(image_features)
        return output
    

class CustomVisEncoder(nn.Module):
    def __init__(self):
        return 

class LGCLIP(nn.Module):
    '''
    Low Granularity CLIP(LGCLIP) --- contrastive learning between image and text embeddings 
    '''
    def __init__(self,
        vision_branch = ImgBranch,
        checkpoint=None,
        vision_checkpoint=None,
        logit_scale_init_value=0.07,
        nntype = None,
        visual_branch_only = False,
        backbone_v = None
        ) -> None:
        super().__init__()
        text_proj_bias = False
        assert vision_branch in [ImgBranch, CustomVisEncoder], 'vision_branch should be one of [ImgBranch]'

        self.vision_model = ImgBranch(nntype = nntype, backbone_v = backbone_v)
        if not visual_branch_only:
          self.text_model = TextBranch(nntype = nntype)

        # learnable temperature for contrastive loss
        self.logit_scale = nn.Parameter(torch.log(torch.tensor(1/logit_scale_init_value)))

        if checkpoint is not None:
            state_dict = torch.load(os.path.join(checkpoint, constants.WEIGHTS_NAME))
            self.load_state_dict(state_dict)
            print('load model weight from:', checkpoint)
        self.nntype = nntype
        self.visual_branch_only = visual_branch_only

    def from_pretrained(self, input_dir=None):
        '''
        If input_dir is None, download pretrained weight from google cloud and load.
        '''
        import wget
        import zipfile
        pretrained_url = None
        if isinstance(self.vision_model, MedCLIPVisionModel):
            # resnet
            pretrained_url = constants.PRETRAINED_URL_MEDCLIP_RESNET
            if input_dir is None:
                input_dir = './pretrained/medclip-resnet'
        elif isinstance(self.vision_model, MedCLIPVisionModelViT):
            # ViT
            pretrained_url = constants.PRETRAINED_URL_MEDCLIP_VIT
            if input_dir is None:
                input_dir = './pretrained/medclip-vit'
        else:
            raise ValueError(f'We only have pretrained weight for MedCLIP-ViT or MedCLIP-ResNet, get {type(self.vision_model)} instead.')

        if not os.path.exists(input_dir):
            os.makedirs(input_dir)

            # download url link
            pretrained_url = requests.get(pretrained_url).text
            filename = wget.download(pretrained_url, input_dir)

            # unzip
            zipf = zipfile.ZipFile(filename)
            zipf.extractall(input_dir)
            zipf.close()
            print('\n Download pretrained model from:', pretrained_url)
        
        state_dict = torch.load(os.path.join(input_dir, constants.WEIGHTS_NAME))
        self.load_state_dict(state_dict)
        print('load model weight from:', input_dir)

    def encode_text(self, inputs_text:list):
        # inputs_text = inputs_text.cuda()
        text_embeds = self.text_model(inputs_text)    # text_feature: backbone generated; text_embedding: processed embeddings
        text_embeds = text_embeds / text_embeds.norm(dim=-1, keepdim=True)
        # torch.save(text_feature, f'text_feature_backbone_{self.nntype}.pth')
        return text_embeds

    def encode_image(self, img_path=None):
        # image encoder
        vision_output = self.vision_model(img_path)   #img_feature: backbone generated; vision_ouput: processed embeddings
        img_embeds = vision_output / vision_output.norm(dim=-1, keepdim=True)
        return img_embeds

    def compute_logits(self, img_emb, text_emb):
        self.logit_scale.data = torch.clamp(self.logit_scale.data, 0, 4.6052)
        logit_scale = self.logit_scale.exp()
        batch = img_emb.shape[0]
        # reshaped_image_embedding = img_emb.view(batch, -1)
        # reshaped_text_embedding =text_emb.view(batch, -1)
        reshaped_text_embedding = text_emb.squeeze()
        reshaped_image_embedding = text_emb
        if  (len(reshaped_image_embedding.shape) == len(reshaped_text_embedding.shape) == 2):
            reshaped_text_embedding = reshaped_text_embedding.unsqueeze(0)
            reshaped_image_embedding = reshaped_image_embedding.unsqueeze(0)
        sim_matrixes = []
        for i, j in zip(reshaped_text_embedding, reshaped_image_embedding):
            sim_matrixes.append(torch.matmul(i, j.t()) * logit_scale)
        return torch.stack(sim_matrixes, dim = 0)   ## each matrix means text-image sim

    def clip_loss(self, similarities: torch.Tensor) -> torch.Tensor:
        batch = 1
        caption_loss = 0
        image_loss = 0
        if len(similarities.shape) == 3 and similarities.shape[0] != 1:
            batch = similarities.shape[0]
        for i in range(batch):
            similarity = similarities[i]
            caption_loss += self.contrastive_loss(similarity)
            image_loss += self.contrastive_loss(similarity.T)
        return (caption_loss + image_loss) / 2.0

    def contrastive_loss(self, logits: torch.Tensor) -> torch.Tensor:
        logits /=  logits.norm(dim=-1, keepdim=True)
        return nn.functional.cross_entropy(logits, torch.arange(len(logits), device=logits.device))
    
    def forward(self,
            input_text:list,
            img_path=None,
            return_loss=True,
            eval = False,
            **kwargs,
            ):
            # input_text = input_text.cuda()/
            loss = 0 # "no applicable in visual branch case"
            text_embeds = 0
            logits_per_image = 0
            img_embeds = self.encode_image(img_path).cuda()
            if eval:
              return {'img_embeds':img_embeds, 'text_embeds':text_embeds,
                'logits_per_image':logits_per_image, 'loss_value':loss}
            if not self.visual_branch_only:
              text_embeds = self.encode_text(input_text).cuda()
              logits_per_image = self.compute_logits(img_embeds, text_embeds) #similarity matrix img2text [0, 1] in multibatch case: the outer matrix contain several inner matrix text-image

              if return_loss:
                  loss = self.clip_loss(logits_per_image)   ## shape [batch, text_sample, image_sample]
            return {'img_embeds':img_embeds, 'text_embeds':text_embeds,
                'logits_per_image':logits_per_image, 'loss_value':loss}

class PN_classifier(nn.Module):
    def __init__(self,
        num_class = 13,
        input_dim=512,
        mode='multiclass',
        num_cat = 3,
        **kwargs) -> None:
        '''args:
        vision_model: the LGCLIP vision branch model that encodes input images into embeddings.
        num_class: number of classes to predict
        input_dim: the embedding dim before the linear output layer
        mode: multilabel, multiclass, or binary
        input number:  the number of input embeddings
        '''
        super().__init__()
        self.num_dim = num_class # each dim corresponding with each disease
        assert mode.lower() in ['multiclass','multilabel','binary']
        self.mode = mode.lower()
        self.num_cat = num_cat
        if num_class > 2:
            if mode == 'multiclass':
                self.loss_fn = nn.CrossEntropyLoss()
            else:
                self.loss_fn = nn.BCEWithLogitsLoss()

            self.fc = nn.Linear(num_class*input_dim, num_class*input_dim)
            self.cls = nn.Linear(num_class*input_dim, num_class * num_cat)
        else:
            self.loss_fn = nn.BCEWithLogitsLoss()
            self.fc = nn.Linear(input_dim, 1)

    def forward(self,
        img_embeddings,  ## original image
        img_label = None,
        return_loss=True,
        multi_CLS = False,
        **kwargs
        ):
        outputs = defaultdict()
        # img_embeddings = img_embeddings.cuda()
        # take embeddings before the projection head
        num_batch = img_embeddings.shape
        num_batch = num_batch[0]
        img_embeddings = img_embeddings.view(num_batch, -1)
        logits = F.relu(self.fc(img_embeddings))
        logits = F.relu(self.fc(logits))
        logits = self.cls(logits)
        outputs['logits'] = logits

        nested_list = img_label
        assert img_label is not None

        if multi_CLS:
            # self.lose_fn_overall = 
            raise NotImplemented("have not implemented")

        if img_label is not None and return_loss:
            if type(img_label[0]) is str:
                nested_list = [json.loads(s) for s in img_label]
            # print(nested_list)
            img_label = torch.tensor(np.stack(nested_list), dtype=torch.long).to(device)
            logits = logits.view(-1, self.num_cat)
            
            if self.mode == 'multiclass': img_label = img_label.flatten().long()
            loss = self.loss_fn(logits, img_label)
            outputs['loss_value'] = loss
        return outputs
    
class Orthogonal_dif(nn.Module):
    '''
    Orthogonal module -- input in predefined text list, pass text branch get n text embeddings. 
    '''
    def __init__(self,
                 logit_scale_init_value = 0.07):
        super().__init__()
        # self.text_module = TextBranch()           
        # learnable temperature for contrastive loss
        self.logit_scale = nn.Parameter(torch.log(torch.tensor(1/logit_scale_init_value)))

    
    def forward(self,
        text_embeds,
        return_loss=True,
        **kwargs,
        ):
        # print("the shape if text_embedding: ",text_embeds.shape)
        loss = 0
        # batch, num_cls, _ = text_embeds.shape
        _len_ = len(text_embeds.shape)
        multi_logits_per_text = []
        # logits_per_text = self.compute_logits(text_embeds, text_embeds) #similarity matrix text2text [0, 1]

        if _len_ == 2:  ## just one sample
          if return_loss:
              logits_per_text =  self.compute_logits(text_embeds)
              loss = self.contrastive_loss(logits_per_text)
              loss += self.contrastive_loss(logits_per_text.T)
          return {'text_embeds':text_embeds,
                  'loss_value':loss, 
                  'multi_logits_per_text':logits_per_text}
        else:   ## multiple samples
          if return_loss:
              for sample in text_embeds:
                logits_each_sample = self.compute_logits(sample)
                multi_logits_per_text.append(logits_each_sample)
                loss += self.contrastive_loss(logits_each_sample)
                loss += self.contrastive_loss(logits_each_sample.T)
              multi_logits = torch.stack(multi_logits_per_text, dim = 0)
          return {'text_embeds':text_embeds,
                  'loss_value':loss, 
                  'multi_logits_per_text':multi_logits}

    def compute_logits(self, emb):
        self.logit_scale.data = torch.clamp(self.logit_scale.data, 0, 4.6052)
        logit_scale = self.logit_scale.exp()
        logits_per_text = torch.matmul(emb, emb.t()) * logit_scale
        # logits_per_text /= logits_per_text.norm(dim=-1, keepdim=True)
        return logits_per_text.t()


    def contrastive_loss(self, logits: torch.Tensor) -> torch.Tensor:
        return nn.functional.cross_entropy(logits, torch.arange(len(logits), device=logits.device))
    

class MultiTaskModel(nn.Module):
    def __init__(self, nntype = "clip", visual_branch_only = False, backbone_v = None):
        super().__init__()
        # print(_constants_.BLUE+"the current backbone nn is: "+_constants_.RESET+nntype)
        # CLIP fashion alignment
        if  (nntype not in ["clip", "biomedclip", "custom"]):
            raise ValueError("currently, only support clip, biomedclip and custom NN")
        if visual_branch_only:
            print(_constants_.CYAN+"current program run in visual branch only version (no contrastive learning between images and text)"+_constants_.RESET)
        self.Contrastive_Model = LGCLIP(nntype = nntype, visual_branch_only = visual_branch_only, backbone_v= backbone_v).to(device)
        self.PN_Classifier = PN_classifier().to(device)
        # img_embedding classifier
        if not visual_branch_only:   ## Orthogonal loss is useless in only visual branch case
          self.Orthogonal_dif = Orthogonal_dif().to(device)
        self.visual_branch_only = visual_branch_only

    def forward(self,         
                prompts:list,
                img = None,
                img_labels = None,
                eval = False):
        assert img is not None
        assert img_labels is not None
        
        a = self.Contrastive_Model(prompts, img, eval=eval)
        b = self.PN_Classifier(a['img_embeds'], img_labels)
        c = 0
        if not eval:
          c = self.Orthogonal_dif(a['text_embeds']) if ((not self.visual_branch_only)) else {"loss_value": 0}
        return a, b, c
    