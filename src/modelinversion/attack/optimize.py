
import os
import importlib
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Tuple, Callable, Optional, Iterable

import torch
from torch import Tensor, LongTensor
from torch.optim import Optimizer, Adam
from tqdm import tqdm

from ..utils import ClassificationLoss
from ..models import BaseImageClassifier, BaseImageGenerator

@dataclass
class BaseImageOptimizationConfig:
    """Base class for all optimization config classes. Handles a few parameters of the experiment settings.
    
    Args:
        experiment_dir (str): The file folder that store the intermediate and final results of the optimization. 
        device (device): The device that the optimization process run on.
    """
    experiment_dir: str
    device: torch.device

class BaseImageOptimization(ABC):
    """Base class for all optimization class. Optimize the initial latent vectors and generate the optimized images.

    Args:
        config (BaseImageOptimizationConfig): Config of the optimization.
    """
    
    def __init__(self, config: BaseImageOptimizationConfig) -> None:
        super().__init__()
        self._config = config
        os.makedirs(config.experiment_dir, exist_ok=True)
    
    @property
    def config(self):
        return self._config
    
    @abstractmethod
    def __call__(self, latents: Tensor, labels: LongTensor) -> Tuple[Tensor, LongTensor]:
        """Optimize the initial latent vectors and generate the optimized images.

        Args:
            latents (Tensor): The latent vectors of the generator.
            labels (LongTensor): The labels for the latent vectors. It has the same length with `latents`.

        Returns:
            Tuple[Tensor, LongTensor]: Returns (images, labels) that has the same length.
        """
        pass
    

@dataclass
class SimpleWhiteBoxOptimizationConfig(BaseImageOptimizationConfig):
    """Base class for all white-box optimization config classes. Handles a few parameters of gradient updating.

    Args:
        optimizer (str | type): The optimizer class.
        optimizer_kwargs (dict): Args to build the optimizer. Default to `{}`.
        iter_times (int): Update times. Defaults to 600.
        show_loss_info_iters (int): Iteration interval for displaying loss information. Default to 100.
    """
    
    optimizer: str | type = 'Adam'
    optimizer_kwargs: dict = field(default_factory=lambda: {})
    iter_times: int = 600
    show_loss_info_iters: int = 100

class SimpleWhiteBoxOptimization(BaseImageOptimization):
    """Base class for all white-box optimization classes.

    Args:
        config (SimpleWhiteBoxOptimizationConfig): 
            Config of the white box optimization.
        generator (BaseImageGenerator): 
            Generator to generate images from latent vectors.
        image_loss_fn (Callable[[Tensor, LongTensor], Tensor | Tuple[Tensor, OrderedDict]]):
            A function to calculate loss of the generated images with given labels. Returns the loss and an optional OrderedDict that contains loss information to show.
    """
    
    def __init__(self, 
                 config: SimpleWhiteBoxOptimizationConfig, 
                 generator: BaseImageGenerator,
                 image_loss_fn: Callable[[Tensor, LongTensor], Tensor | Tuple[Tensor, OrderedDict]]
                 ) -> None:
        super().__init__(config)
        
        optimizer_class = config.optimizer
        if isinstance(optimizer_class, str):
            optim_module = importlib.import_module('torch.optim')
            optimizer_class = getattr(optim_module, optimizer_class, None)
            
        if not hasattr(optimizer_class, 'zero_grad'):
            raise RuntimeError('Optimizer do not has attribute `zero_grad`')
        
        if not hasattr(optimizer_class, 'step'):
            raise RuntimeError('Optimizer do not has attribute `step`')
        
        self.optimizer_class = optimizer_class
        self.generator = generator
        self.image_loss_fn = image_loss_fn
        
    def __call__(self, latents: Tensor, labels: LongTensor) -> Tuple[Tensor, LongTensor]:
        
        config: SimpleWhiteBoxOptimizationConfig = self.config
        
        latents = latents.to(config.device)
        labels = labels.to(config.device)
        latents.requires_grad_(True)
        optimizer: Optimizer = self.optimizer_class([latents], **config.optimizer_kwargs)
        
        bar = tqdm(range(1, config.iter_times + 1), leave=False)
        for i in bar:
            
            fake = self.generator(latents, labels=labels)
            # if config.image_initial_transform is not None:
            #     fake = config.image_initial_transform(fake)
                
            loss = self.image_loss_fn(fake, labels)
            if isinstance(loss, tuple):
                loss, metric_dict = loss
                if metric_dict is not None and len(metric_dict) > 0 and (i == 1 or i % config.show_loss_info_iters == 0):
                    ls = [f'{k}: {v}' for k, v in metric_dict.items()]
                    right_str = '  '.join(ls)
                    description = f'iter {i}: {right_str}'
                    bar.set_description_str(description)
                    
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            
        final_fake = self.generator(latents, labels=labels).cpu()
        
        # if config.image_initial_transform is not None:
        #     final_fake = config.image_initial_transform(final_fake)
                
        final_labels = labels
        
        return final_fake, final_labels
    
@dataclass
class ImageAugmentWhiteBoxOptimizationConfig(SimpleWhiteBoxOptimizationConfig):
    
    loss_fn: str | Callable[[Tensor, LongTensor], Tensor] = 'cross_entropy'
    # initial_transform: Optional[Callable] = None, 
    create_aug_images_fn: Optional[Callable[[Tensor], Iterable[Tensor]]]=None
        
class ImageAugmentWhiteBoxOptimization(SimpleWhiteBoxOptimization):
    
    def __init__(self, 
                 config: ImageAugmentWhiteBoxOptimizationConfig, 
                 generator: BaseImageGenerator,
                 target_model: BaseImageClassifier
                 ) -> None:
        
        if config.create_aug_images_fn is None:
            config.create_aug_images_fn = lambda x: [x]
            
        # print(config.loss_fn)
        
        loss_fn = ClassificationLoss(config.loss_fn)

        def _image_loss_fn(images: Tensor, labels: LongTensor):
            
            # if config.initial_transform is not None:
            #     images = config.initial_transform(images)
            
            acc = 0
            loss = 0
            total_num = 0
            for aug_images in config.create_aug_images_fn(images):
                total_num += 1
                conf = target_model(aug_images)
                pred_labels = torch.argmax(conf, dim=-1)
                loss += loss_fn(conf, labels)
                # print(pred_labels)
                # print(labels)
                # exit()
                acc += (pred_labels == labels).float().mean().item()
            
            return loss, OrderedDict([
                ['loss', loss.item()],
                ['target acc', acc]
            ])
            
        
        super().__init__(config, generator, _image_loss_fn)