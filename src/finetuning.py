
import clip
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset, random_split

from typing import Tuple
from PIL import Image
import pickle
import os

from preprocessing import ImageAugmenter, TextAugmenter


device = "cuda" if torch.cuda.is_available() else "cpu"



class ImageCaptionDataset(Dataset):

    def __init__(self, images_dir: str = "images/imagesf2", captions_file: str = "artwork_captions.txt"):
        """
        Initializes the ImageTextDataset.
        """
        self.mode = "train"
        self._images_dir = os.path.join("data", images_dir)
        self._captions_file = os.path.join("data", captions_file)

        self._image_augmenter = ImageAugmenter()
        self._text_augmenter = TextAugmenter()

        with open(self._captions_file, "r") as f:
            lines = f.readlines()
        
        self._image_caption_pairs = [(line.split("\t")[0], line.split("\t")[1].strip()) for line in lines]
    

    def __len__(self):
        """
        Returns the number of image-caption pairs in the dataset.

        Returns:
            int: The number of image-caption pairs.
        """
        return len(self._image_caption_pairs)

    def __getitem__(self, idx):
        """
        Returns the image and text at a given index.

        Args:
            idx (int): The index of the image and text to be returned.

        Returns:
            tuple: A tuple containing the image and text.
        """
        image_path, text = self._image_caption_pairs[idx]        
        image_path = os.path.join(self._images_dir, image_path)
        image = Image.open(image_path).convert("RGB")

        if self.mode == "train":
            image = self._image_augmenter(image)
            text = self._text_augmenter(text)

        return image, text



class CLIPFinetuner:

    def __init__(self, model_name: str = "ViT-B/32", dataset: Dataset = ImageCaptionDataset(), val_split: float = .3,
                 batch_size: int = 128, lr: float = 5e-5, unfreeze_from: int = 6, unfreeze_every: int = 2):
        """
        Initializes the CLIPFinetuner.
        """
        self._model, _ = clip.load(model_name, device=device, jit=False)
        self._model.float()
        self._tot_blocks = len(self._model.visual.transformer.resblocks)
        self._freeze_model()
        self._unfreeze_blocks(1)

        self._dataset = dataset

        self._unfreeze_from = unfreeze_from
        self._unfreeze_every = unfreeze_every

        self._early_stopping = EarlyStopping(self._model)

        self._train_loader, self._val_loader = self._get_data_loaders(val_split, batch_size)
        self._optimizer = optim.Adam(self._model.parameters(), lr=lr, betas=(0.9, 0.98), eps=1e-6, weight_decay=.2)


    def get_model(self) -> torch.nn.Module:
        """
        Returns the model.

        Returns:
            torch.nn.Module: The model.
        """
        return self._model

    def fit(self, epochs: int = 100, verbose: bool = True) -> None:
        """
        Trains the model for the given number of epochs.

        Args:
            epochs (int, optional): The number of epochs to train the model. Defaults to 100.
            verbose (bool, optional): Whether to print the training and validation losses and scores during training. Defaults to True.

        Returns:
            None
        """
        for epoch in range(epochs):
            self._partial_unfreeze(epoch)
            train_loss = self._train()
            val_loss, val_score = self._validate()

            if verbose:
                print(f"\nEpoch #{epoch+1}/{epochs} [")
                print(f"Train Loss: {train_loss:.4f}")
                print(f"Val Loss: {val_loss:.4f}, Val Score: {val_score:.4f}\n]")
            
            stop = self._early_stopping(train_loss, val_loss, val_score)
            if stop:
                if verbose:
                    print(f"Early stopping at epoch #{epoch+1}")
                break

    def _get_data_loaders(self, val_split: float = .3, batch_size: int = 128) -> Tuple[DataLoader]:
        """
        Returns the train and validation DataLoaders.

        Args:
            val_split (float, optional): The proportion of the dataset to include in the validation set. Defaults to .3.
            batch_size (int, optional): The batch size for the DataLoaders. Defaults to 128.

        Returns:
            Tuple[DataLoader]: A tuple containing the train and validation DataLoaders.
        """
        train_size = int((1 - val_split) * len(self._dataset))
        val_size = len(self._dataset) - train_size
        train_dataset, val_dataset = random_split(self._dataset, [train_size, val_size])

        train_dataset.mode = "train"
        val_dataset.mode = "val"
        train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)
        val_loader = DataLoader(val_dataset, batch_size=batch_size, shuffle=False)

        return train_loader, val_loader

    def _freeze_model(self) -> None:
        """
        Freeze all the model's parameters.
        """
        for p in self._model.parameters():
            p.requires_grad_(False)

    def _unfreeze_blocks(self, blocks_to_unfreeze: int) -> None:
        """
        Unfreeze the last given number of transformer blocks in the model.

        Args:
            blocks_to_unfreeze (int): The number of blocks to unfreeze.

        Returns:
            None
        """
        self._model.visual.proj.requires_grad_()
        self._model.text_projection.requires_grad_()

        if blocks_to_unfreeze > self._tot_blocks:
            blocks_to_unfreeze = self._tot_blocks

        for i in range(self._tot_blocks - blocks_to_unfreeze, self._tot_blocks):
            for p in self._model.visual.transformer.resblocks[i].parameters():
                p.requires_grad_()

        for i in range(self._tot_blocks - blocks_to_unfreeze, self._tot_blocks):
            for p in self._model.transformer.resblocks[i].parameters():
                p.requires_grad_()

    def _partial_unfreeze(self, epoch: int) -> None:
        """
        Partially unfreeze the model, given the epoch.

        Every self._unfreeze_every epochs, starting from self._unfreeze_from, unfreeze one more transformer block.

        Args:
            epoch (int): The current epoch.

        Returns:
            None
        """
        epoch += 1

        if epoch >= self._unfreeze_from and epoch % self._unfreeze_every == 0:
            blocks_to_unfreeze = (epoch - self._unfreeze_from + self._unfreeze_every) // self._unfreeze_every
            blocks_to_unfreeze += 1

            if blocks_to_unfreeze <= self._tot_blocks:
                self._unfreeze_blocks(blocks_to_unfreeze)
                print(f"Unfreezing blocks: {blocks_to_unfreeze}/{self._tot_blocks}.\n")

    def _clip_score(self, images: torch.Tensor, texts: torch.Tensor) -> float:
        """
        Returns the CLIP score for the given images and texts.

        Args:
            images (torch.Tensor): The images.
            texts (torch.Tensor): The texts.

        Returns:
            float: The CLIP score.
        """
        with torch.no_grad():
            image_features = self._model.encode_image(images)
            text_features = self._model.encode_text(texts)

        image_features = image_features / image_features.norm(dim=-1, keepdim=True)
        text_features = text_features / text_features.norm(dim=-1, keepdim=True)
        score = (image_features @ text_features.t()).diag().mean()

        return score

    def _train(self) -> Tuple[float]:
        """
        Train the model on the training set for one epoch.

        Returns:
            Tuple[float]: The average training loss and score.
        """
        self._model.train()

        total_loss = .0

        for images, texts in self._train_loader:
            images = images.to(device)
            texts = clip.tokenize(texts).to(device)

            logits_per_image, logits_per_text = self._model(images, texts)
            ground_truth = torch.arange(len(images), dtype=torch.long).to(device)

            loss_img = F.cross_entropy(logits_per_image, ground_truth)
            loss_txt = F.cross_entropy(logits_per_text, ground_truth)
            loss = (loss_img + loss_txt) / 2

            self._optimizer.zero_grad()
            loss.backward()
            self._optimizer.step()

            total_loss += loss.item()

        total_loss /= len(self._train_loader)
        return total_loss

    def _validate(self) -> Tuple[float]:
        """
        Validate the model on the validation set.

        Returns:
            Tuple[float]: The average validation loss and score.
        """
        self._model.eval()

        total_loss = .0
        total_score = .0

        with torch.no_grad():
            for images, texts in self._val_loader:
                images = images.to(device)
                texts = clip.tokenize(texts).to(device)

                logits_per_image, logits_per_text = self._model(images, texts)
                ground_truth = torch.arange(len(images), dtype=torch.long).to(device)

                loss_img = F.cross_entropy(logits_per_image, ground_truth)
                loss_txt = F.cross_entropy(logits_per_text, ground_truth)
                loss = (loss_img + loss_txt) / 2

                score = self._clip_score(images, texts)
                total_score += score.item()
                total_loss += loss.item()

        total_score /= len(self._val_loader)
        total_loss /= len(self._val_loader)
        return total_loss, total_score



class EarlyStopping:

    def __init__(self, model: nn.Module, patience: int = 50, dir_path: str = "models", mode: str = "max"):
        """
        Initialize the early stopping object.
        """
        self._model = model
        self._patience = patience
        self._best_score = None
        self._counter = 0
        self._mode = mode
        self._stop = False
        self._dir_path = dir_path

        self._train_loss = []
        self._val_loss = []
        self._val_scores = []

    def __call__(self, train_loss: float, val_loss: float, val_score: float) -> bool:
        """
        Call the early stopping object.

        Args:
            train_loss (float): The training loss.
            val_loss (float): The validation loss.
            val_score (float): The validation score.

        Returns:
            bool: True if the early stopping criteria is met, False otherwise.
        """
        self._train_loss.append(train_loss)
        self._val_loss.append(val_loss)
        self._val_scores.append(val_score)

        score = val_score

        if self._best_score is None:
            self._best_score = score
        elif self._is_improvement(score):
            self._save_checkpoint()
            self._best_score = score
            self._counter = 0
        else:
            self._counter += 1
            if self._counter >= self._patience:
                self._stop = True
        
        return self._stop

    def _is_improvement(self, score: float) -> bool:
        """
        Check if the score is an improvement.

        Args:
            score (float): The score to check.

        Returns:
            bool: True if the score is an improvement, False otherwise.
        """
        if self._mode == "max":
            return score > self._best_score
        else:
            return score < self._best_score

    def _save_checkpoint(self, name: str = "checkpoint.pt") -> None:
        """
        Save the model checkpoint.

        Args:
            name (str, optional): The name of the checkpoint. Defaults to "checkpoint.pt".

        Returns:
            None
        """
        checkpoint_path = os.path.join(self._dir_path, name)
        torch.save(self._model.state_dict(), checkpoint_path)
    
    def _save_lists(self) -> None:
        """
        Save the loss and score lists with pickle.

        Args:
            None

        Returns:
            None
        """
        loss_path = os.path.join(self._dir_path, "loss.pkl")
        score_path = os.path.join(self._dir_path, "score.pkl")
        with open(loss_path, "wb") as f:
            pickle.dump((self._train_loss, self._val_loss), f)
        with open(score_path, "wb") as f:
            pickle.dump(self._val_scores, f)
